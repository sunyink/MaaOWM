#!/usr/bin/env python3
"""
MaaOWM V3 — Overlay Workspace Manager (基于 MaaFW Oracle)

V3 核心改动:
  - 委托 MaaFW PipelineDumper 做语义合并, 不再自己实现字段级 diff
  - canonical 比对天然支持 V1/V2 混用 (输出统一为 V2)
  - mount 时存 base 快照, unmount 时用快照减数 (与 git pull 解耦)
  - 不再处理 image/model (直接 passthrough)
"""

from __future__ import annotations

import sys
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

try:
    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text
    from rich.prompt import Prompt, Confirm
    from rich import box
except ImportError:
    print("错误: 缺少 rich 库。请运行: pip install rich")
    sys.exit(1)

from core import config as config_mod
from core import diff
from core import inplace


VERSION = "0.4.0"

STATE_UNMOUNTED = "未挂载"
STATE_MOUNTED = "已挂载"

HELP_TEXT = """\
[bold cyan]MaaOWM V3 — Overlay Workspace Manager[/bold cyan]

V3 委托 MaaFW 自己的 PipelineDumper 做语义合并, 你的 mod 包永远跟运行时
真实合并行为对齐, 不会因为 MaaFW 升级或字段细节不同步而漂移。

[bold]工作流程:[/bold]
  1. 准备 overlay_config.json
  2. [M] 挂载 — 备份 mod, 把 base+mod 合并写入 mod 作为工作区
  3. 用 MaaPipelineEditor 打开 mod 控制器编辑
  4. 编辑完毕 → [U] 卸载 — diff 提取 minimal mod, 写回 mod 包

[bold]配置文件示例:[/bold]
  {
      "target": "assets/resource/PC",
      "base_layers": ["assets/resource/base"],
      "maa_pkg_dir": null
  }

[bold]maa_pkg_dir:[/bold]
  null    自动从 import maa 取包目录 (默认, 推荐)
  路径    显式指向 site-packages/maa 级别 (覆盖自动检测)

[bold]工作区编辑准则 (重要):[/bold]
  ✓ 改字段值 / 加新字段 / 新建 task
  ✗ 不要从 task 删字段 (独立加载会退默认, 与意图不符)
  ✗ 不要随便删被引用的 task (改 enabled:false 替代)
  详见挂载后工作区根目录的 __OWM_README__.md

[bold]V3 限制:[/bold]
  - 输出 mod 永远是 V2 格式 (历史 V1 task 经 round-trip 会现代化)
  - sub-object (recognition/action/attach 等) 字段级变化 → 整段写入
"""


class OverlayToolApp:
    def __init__(self, config_path: str | Path):
        self.console = Console()
        self.config_path = Path(config_path).resolve()
        self.config: config_mod.OverlayConfig | None = None
        self.state: str = STATE_UNMOUNTED

    def _detect_state(self) -> str:
        if self.config and inplace.is_mounted(self.config):
            return STATE_MOUNTED
        return STATE_UNMOUNTED

    def _render_header(self):
        mounted = self.state == STATE_MOUNTED
        state_style = "bold green" if mounted else "bold yellow"
        state_label = STATE_MOUNTED if mounted else STATE_UNMOUNTED

        title = Text()
        title.append("MaaOWM V3 ", style="bold white")
        title.append(f"v{VERSION}", style="dim")
        title.append("  ", style="")
        title.append("Oracle-based", style="bold magenta")

        status = Text()
        status.append("状态: ", style="bold")
        status.append(state_label, style=state_style)

        if mounted and self.config:
            info = inplace.get_mount_info(self.config)
            if info:
                status.append(f"  ({info['mount_ts_readable']} 挂载, ", style="dim")
                status.append(f"{info['task_count']} task", style="cyan")
                status.append(")", style="dim")

        if self.config:
            cfg = self.config
            status.append("\n")
            status.append("Target : ", style="dim")
            status.append(str(cfg.target_path), style="white")
            status.append("\nBase   : ", style="dim")
            status.append("  ".join(str(p) for p in cfg.base_layer_paths()), style="white")
            if cfg.maa_pkg_dir:
                status.append("\nmaa    : ", style="dim")
                status.append(str(cfg.maa_pkg_dir) + " (显式)", style="cyan")
            status.append("\n输出   : ", style="dim")
            if cfg.output_format == "v1":
                status.append("V1", style="bold magenta")
                status.append("  (拍平 / 省略默认)", style="dim magenta")
            else:
                status.append("V2", style="bold cyan")
                status.append("  (嵌套 / 全字段)", style="dim cyan")

        content = Text()
        content.append_text(title)
        content.append("\n")
        content.append_text(status)

        self.console.print(Panel(content, box=box.ROUNDED, border_style="blue"))

    def _render_menu(self):
        mounted = self.state == STATE_MOUNTED
        table = Table(show_header=False, box=None, padding=(0, 2), expand=False)
        table.add_column(style="bold cyan", width=4)
        table.add_column()

        items = []
        if not mounted:
            items.append(("M", "挂载", "备份 mod, base+mod 合并写入工作区"))
            items.append(("V", "切换输出格式", "V2 ↔ V1 (仅未挂载时可切)"))
        else:
            items.append(("U", "卸载", "diff 提取 minimal mod, 写回 mod 包"))
        items += [
            ("L", "查看日志", "operations.log 最近记录"),
            ("B", "查看备份", ".maaowm/ 中的备份列表"),
            ("H", "使用说明", ""),
            ("0", "退出", ""),
        ]

        for key, label, desc in items:
            row = label + (f"  [dim]— {desc}[/dim]" if desc else "")
            table.add_row(f"[{key}]", row)

        self.console.print(table)
        self.console.print()

    def action_mount(self):
        assert self.config is not None
        self.console.print("\n[bold]━━━ 挂载 ━━━[/bold]\n")
        fmt = self.config.output_format
        fmt_label = (
            "[magenta]V1[/magenta] (拍平 / 省略默认)"
            if fmt == "v1"
            else "[cyan]V2[/cyan] (嵌套 / 全字段)"
        )
        self.console.print(
            "[yellow]注意[/yellow] 挂载将备份当前 mod 包, 然后用 base+mod 合并的 canonical "
            "全字段内容覆盖。\n"
            "  若 mod 在 Git 仓库中, git status 会出现大量变更, 属正常现象。\n"
            "  建议挂载前先 commit 当前状态。\n"
            f"  当前输出格式: {fmt_label}\n"
        )
        if not Confirm.ask("确认继续挂载?", default=True):
            self.console.print("[yellow]操作取消[/yellow]")
            return

        try:
            result = inplace.mount(
                self.config,
                progress_callback=lambda m: self.console.print(f"  [dim]→ {m}[/dim]"),
            )
        except inplace.MountError as e:
            self.console.print(f"\n[red]✗ 挂载失败:[/red] {e}")
            return

        if result.warnings:
            self.console.print("[yellow]警告:[/yellow]")
            for w in result.warnings:
                self.console.print(f"  [yellow]![/yellow] {w}")

        self.console.print(f"\n[green]✓ 挂载完成[/green]  {result.summary()}")
        self.console.print(
            f"  备份: [dim].maaowm/{result.og_backup}[/dim]\n"
            "  [cyan]现可用 MaaPipelineEditor 打开 mod 控制器编辑。[/cyan]\n"
            "  [cyan]编辑完毕选 [U] 卸载。[/cyan]"
        )
        self.state = STATE_MOUNTED

    def action_unmount(self):
        assert self.config is not None
        self.console.print("\n[bold]━━━ 卸载 ━━━[/bold]\n")
        fmt = self.config.output_format
        fmt_label = (
            "[magenta]V1[/magenta] (拍平 / 省略默认)"
            if fmt == "v1"
            else "[cyan]V2[/cyan] (嵌套 / 全字段)"
        )
        self.console.print(f"  当前输出格式: {fmt_label}\n")
        if not Confirm.ask("确认继续卸载?", default=True):
            self.console.print("[yellow]操作取消[/yellow]")
            return

        try:
            result = inplace.unmount(
                self.config,
                progress_callback=lambda m: self.console.print(f"  [dim]→ {m}[/dim]"),
            )
        except inplace.UnmountError as e:
            self.console.print(f"\n[red]✗ 卸载失败:[/red] {e}")
            return

        if result.warnings:
            self.console.print("[yellow]警告:[/yellow]")
            for w in result.warnings:
                self.console.print(f"  [yellow]![/yellow] {w}")

        self.console.print(f"\n[green]✓ 卸载完成[/green]  {result.summary()}")

        if result.hints:
            self.console.print("\n[bold]Hints:[/bold]")
            for h in result.hints:
                style = "yellow" if h.severity == "warn" else "blue"
                sym = "⚠" if h.severity == "warn" else "ℹ"
                self.console.print(f"  [{style}]{sym}[/{style}] [bold]{h.task}[/bold]")
                for line in h.text.splitlines():
                    self.console.print(f"     [{style}]{line}[/{style}]")

        self.console.print(
            f"\n  工作区备份: [dim].maaowm/{result.work_backup}[/dim]\n"
            "  [cyan]mod 已恢复为 minimal 增量, 请用 Git 检查并提交。[/cyan]"
        )
        self.state = STATE_UNMOUNTED

    def action_view_log(self):
        assert self.config is not None
        self.console.print("\n[bold]━━━ 操作日志 (最近 50 条) ━━━[/bold]\n")
        lines = inplace.get_log_lines(self.config)
        if not lines:
            self.console.print("[dim]暂无日志记录。[/dim]")
            return
        for line in lines:
            if "[MOUNT-OK]" in line:
                self.console.print(f"  [green]{line}[/green]")
            elif "[UNMOUNT-OK]" in line:
                self.console.print(f"  [cyan]{line}[/cyan]")
            elif "-FAIL" in line:
                self.console.print(f"  [red]{line}[/red]")
            else:
                self.console.print(f"  [dim]{line}[/dim]")

    def action_view_backups(self):
        assert self.config is not None
        self.console.print("\n[bold]━━━ 备份列表 ━━━[/bold]\n")
        backups = inplace.get_backup_list(self.config)
        if not backups:
            self.console.print("[dim]暂无备份。[/dim]")
            return
        tbl = Table(box=box.SIMPLE_HEAD, show_edge=False, pad_edge=False, padding=(0, 1))
        tbl.add_column("时间", style="dim", no_wrap=True)
        tbl.add_column("类型", no_wrap=True)
        tbl.add_column("目录名", style="white")
        for b in backups:
            kind_style = "yellow" if "og" in b["kind"] else "cyan"
            tbl.add_row(b["mtime_str"], f"[{kind_style}]{b['kind']}[/]", b["name"])
        self.console.print(tbl)
        self.console.print(f"\n  [dim]备份位置: {self.config.owm_dir}[/dim]")

    def action_help(self):
        self.console.print()
        self.console.print(Panel(HELP_TEXT, title="使用说明", border_style="cyan", expand=False))

    def action_toggle_format(self):
        """切换 V2 ↔ V1 输出格式 (仅未挂载时可切)。"""
        assert self.config is not None
        self.console.print("\n[bold]━━━ 切换输出格式 ━━━[/bold]\n")
        cur = self.config.output_format
        new = "v1" if cur == "v2" else "v2"

        cur_label = "[cyan]V2[/cyan] (嵌套 / 全字段)" if cur == "v2" else "[magenta]V1[/magenta] (拍平 / 省略默认)"
        new_label = "[magenta]V1[/magenta] (拍平 / 省略默认)" if new == "v1" else "[cyan]V2[/cyan] (嵌套 / 全字段)"

        self.console.print(f"  当前: {cur_label}")
        self.console.print(f"  目标: {new_label}\n")

        if new == "v1":
            self.console.print(
                "[yellow]提示[/yellow] V1 模式将影响下次挂载和卸载的产物形态:\n"
                "  • 工作区文件: recognition/action 字段拍平到 task 顶层\n"
                "  • mod 产物:   同样拍平形态\n"
                "  • 默认 type (DirectHit/DoNothing) 整段省略\n"
                "  此切换不会修改当前已挂载的工作区 — 下次挂载才生效。\n"
            )

        if not Confirm.ask(f"确认切换为 {new.upper()}?", default=True):
            self.console.print("[yellow]操作取消[/yellow]")
            return

        try:
            config_mod.set_output_format_in_config(self.config_path, new)
        except Exception as e:
            self.console.print(f"[red]✗ 写入配置失败: {e}[/red]")
            return

        # 重新加载 config 让本次会话生效
        self.config = config_mod.load_config(self.config_path)
        self.console.print(
            f"[green]✓ 已切换为 {new.upper()}[/green]  "
            f"[dim](写入 {self.config_path.name}, 下次挂载/卸载生效)[/dim]"
        )

    def run(self):
        self.console.clear()
        self.console.print(f"[dim]配置文件: {self.config_path}[/dim]\n")

        if not self.config_path.exists():
            self.console.print(f"[yellow]配置文件不存在: {self.config_path}[/yellow]\n")
            if Confirm.ask("是否生成示例配置?", default=True):
                config_mod.write_sample_config(self.config_path)
                self.console.print(f"[green]✓[/green] 示例已写入 {self.config_path}")
                self.console.print("[dim]请按项目实际路径修改后重新运行。[/dim]")
            return

        try:
            self.config = config_mod.load_config(self.config_path)
        except config_mod.ConfigError as e:
            self.console.print(f"[red]配置加载失败: {e}[/red]")
            return

        errs = self.config.validate()
        if errs:
            self.console.print("[red]配置校验失败:[/red]")
            for err in errs:
                self.console.print(f"  [red]✗[/red] {err}")
            return

        self.state = self._detect_state()

        while True:
            self.console.print()
            self._render_header()
            self.console.print()
            self._render_menu()

            mounted = self.state == STATE_MOUNTED
            if mounted:
                choices = ["U", "L", "B", "H", "0"]
            else:
                choices = ["M", "V", "L", "B", "H", "0"]
            choice = Prompt.ask("选择操作", choices=choices, default="0").upper()

            if choice == "M":
                self.action_mount()
            elif choice == "U":
                self.action_unmount()
            elif choice == "V":
                self.action_toggle_format()
            elif choice == "L":
                self.action_view_log()
            elif choice == "B":
                self.action_view_backups()
            elif choice == "H":
                self.action_help()
            elif choice == "0":
                self.console.print("\n[dim]再见![/dim]")
                break


def main():
    if len(sys.argv) > 1:
        config_path = Path(sys.argv[1])
    else:
        cwd_config = Path.cwd() / "overlay_config.json"
        script_config = _SCRIPT_DIR / "overlay_config.json"
        config_path = cwd_config if cwd_config.exists() else (
            script_config if script_config.exists() else cwd_config
        )

    OverlayToolApp(config_path).run()


if __name__ == "__main__":
    main()
