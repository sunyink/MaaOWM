#!/usr/bin/env python3
"""
MFABD2 覆盖包工作区管理器 (Overlay Workspace Manager)

解决 MaaFramework V2 跨端适配中的"编辑器全量覆盖"问题。
通过正向合并生成完整工作区，逆向语义 Diff 提取干净增量。

使用方式:
    python overlay_tool.py                        # 自动查找当前目录下的 overlay_config.json
    python overlay_tool.py /path/to/config.json   # 指定配置文件路径
"""

import sys
import shutil
from pathlib import Path

# 确保脚本所在目录在搜索路径中（解决工作目录 ≠ 脚本目录的问题）
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

from core.config import load_config, OverlayConfig
from core.merger import merge_to_workspace, MergeResult
from core.differ import compute_diff, DiffResult
from core.writer import write_back, WriteResult

# ============================================================
#  常量与状态
# ============================================================

VERSION = "0.1.2"

STATE_IDLE = "空闲"
STATE_LOADED = "配置已加载"
STATE_WORKSPACE_READY = "工作区就绪"
STATE_DIFF_READY = "差异已生成"

HELP_TEXT = """\
[bold cyan]使用说明[/bold cyan]

本工具用于管理 MaaFramework 项目的跨端覆盖包开发流程。

[bold]工作流程:[/bold]
  1. 准备配置文件 overlay_config.json，定义 base 层和 target 覆盖层
  2. [挂载工作区] 将多层资源正向合并到临时工作区
  3. 使用编辑器（MaaPipelineEditor 等）打开工作区进行编辑
  4. 编辑完成后 [刷新差异预览] 查看变更摘要
  5. 确认无误后 [确认回写] 将干净增量写回覆盖包目录
  6. [卸载工作区] 清理临时目录

[bold]配置文件示例:[/bold]
  {
      "workspace_dir": ".workspace",
      "target": "PC",
      "base_layers": ["base"],
      "resource_types": ["pipeline", "image", "model"]
  }

[bold]路径规则:[/bold]
  - 支持绝对路径和相对路径（相对于配置文件所在目录）
  - 支持 ../ 向上导航，例如工具在 tools/ 下而资源在 assets/ 下：
    "target": "../assets/resource/PC"
  - base_layers 数组中，后面的覆盖前面的
  - target 最后覆盖合并完毕的 base

[bold]注意事项:[/bold]
  - 工作区活跃期间请勿切换 Git 分支修改 base 层
  - 如需禁用整个节点，请手动在覆盖包中添加 "enabled": false
  - Pipeline Diff 基于语义比对，兼容 v1 扁平和 v2 嵌套结构\
"""

# ============================================================
#  TUI 应用
# ============================================================


class OverlayToolApp:
    """覆盖包工作区管理器 TUI 应用。"""

    def __init__(self, config_path: str | Path):
        self.console = Console()
        self.config_path = Path(config_path).resolve()
        self.config: OverlayConfig | None = None
        self.state: str = STATE_IDLE
        self.diff_result: DiffResult | None = None

    # --- 状态渲染 ---

    def _render_header(self):
        """渲染标题栏。"""
        state_color = {
            STATE_IDLE: "dim",
            STATE_LOADED: "yellow",
            STATE_WORKSPACE_READY: "green",
            STATE_DIFF_READY: "cyan",
        }.get(self.state, "white")

        title = Text()
        title.append("MFABD2 覆盖包工作区管理器", style="bold white")
        title.append(f"  v{VERSION}", style="dim")

        status = Text()
        status.append("状态: ", style="bold")
        status.append(self.state, style=f"bold {state_color}")

        if self.config:
            status.append("  |  ", style="dim")
            status.append(f"配置: {self.config_path.name}", style="dim")
            status.append(f"  Target: {self.config.target}", style="dim")

        content = Text()
        content.append_text(title)
        content.append("\n")
        content.append_text(status)

        self.console.print(
            Panel(content, box=box.ROUNDED, border_style="blue")
        )

    def _render_menu(self):
        """渲染操作菜单。"""
        table = Table(
            show_header=False, box=None, padding=(0, 2), expand=False
        )
        table.add_column(style="bold cyan", width=4)
        table.add_column()

        available = self._get_available_actions()

        menu_items = [
            ("1", "挂载工作区", "读取配置，正向合并，生成工作区"),
            ("2", "刷新差异预览", "对比工作区和 base，展示变更摘要"),
            ("3", "确认回写", "将差异写回覆盖包目录"),
            ("4", "卸载工作区", "清理临时目录，回到空闲状态"),
            ("h", "使用说明", "查看完整帮助文档"),
            ("0", "退出", ""),
        ]

        for key, label, desc in menu_items:
            if key in available:
                style = ""
                desc_style = "dim"
            else:
                style = "dim strikethrough"
                desc_style = "dim strikethrough"

            row_text = f"{label}"
            if desc:
                row_text += f"  [dim]— {desc}[/dim]"

            table.add_row(f"[{key}]", f"[{style}]{row_text}[/]" if style else row_text)

        self.console.print(table)
        self.console.print()

    def _get_available_actions(self) -> set:
        """根据当前状态返回可用的操作。"""
        always = {"h", "0"}

        if self.state == STATE_IDLE:
            return always | {"1"}
        elif self.state == STATE_LOADED:
            return always | {"1"}
        elif self.state == STATE_WORKSPACE_READY:
            return always | {"2", "4"}
        elif self.state == STATE_DIFF_READY:
            return always | {"2", "3", "4"}

        return always

    # --- Diff 摘要渲染 ---

    def _render_diff_summary(self, diff_result: DiffResult):
        """用 Rich Table 渲染差异摘要，高亮变更项并列对齐。"""

        has_any_output = False

        # === Pipeline 摘要表 ===
        if diff_result.pipeline_diffs:
            has_any_output = True

            tbl = Table(
                title="[bold]Pipeline[/bold]",
                title_style="",
                box=box.SIMPLE_HEAD,
                show_edge=False,
                pad_edge=False,
                padding=(0, 1),
                expand=False,
            )
            tbl.add_column("", width=2, no_wrap=True)                       # 状态标记
            tbl.add_column("文件", style="white", no_wrap=True)             # 文件路径
            tbl.add_column("修改", justify="right", no_wrap=True)           # 修改数
            tbl.add_column("新增", justify="right", no_wrap=True)           # 新增数
            tbl.add_column("无变化", justify="right", style="dim", no_wrap=True)

            total_mod = total_new = total_unch = 0

            for rel_path, diff_info in diff_result.pipeline_diffs.items():
                mod = len(diff_info.modified_nodes)
                new = len(diff_info.new_nodes)
                unch = diff_info.unchanged_count
                total_mod += mod
                total_new += new
                total_unch += unch

                # 状态标记：有变更为亮色圆点，无变更为暗色圆圈
                if diff_info.has_changes:
                    marker = "[bold yellow]●[/]"
                else:
                    marker = "[dim]○[/]"

                # 修改列：有修改时高亮黄色加粗
                mod_cell = f"[bold yellow]{mod}[/]" if mod else "[dim]-[/]"
                # 新增列：有新增时高亮绿色加粗
                new_cell = f"[bold green]{new}[/]" if new else "[dim]-[/]"
                # 无变化列
                unch_cell = str(unch) if unch else "-"

                tbl.add_row(marker, rel_path, mod_cell, new_cell, unch_cell)

            # 合计行
            tbl.add_section()
            total_mod_cell = f"[bold yellow]{total_mod}[/]" if total_mod else "[dim]0[/]"
            total_new_cell = f"[bold green]{total_new}[/]" if total_new else "[dim]0[/]"
            total_unch_cell = f"[dim]{total_unch}[/]"
            tbl.add_row(
                "",
                "[bold]合计[/]",
                total_mod_cell,
                total_new_cell,
                total_unch_cell,
            )

            self.console.print(tbl)
            self.console.print()

        # === Image / Model 摘要 ===
        for res_type, bin_diff in [
            ("Image", diff_result.image_diff),
            ("Model", diff_result.model_diff),
        ]:
            if not (bin_diff.new_files or bin_diff.modified_files or bin_diff.unchanged_files):
                continue

            has_any_output = True
            parts = []
            if bin_diff.modified_files:
                parts.append(f"[bold yellow]修改 {len(bin_diff.modified_files)}[/]")
            if bin_diff.new_files:
                parts.append(f"[bold green]新增 {len(bin_diff.new_files)}[/]")
            if bin_diff.unchanged_files:
                parts.append(f"[dim]剔除 {len(bin_diff.unchanged_files)}[/]")
            self.console.print(f"  [bold]{res_type}[/]  {' │ '.join(parts)}")

        # === 错误 ===
        if diff_result.errors:
            has_any_output = True
            self.console.print(f"\n  [bold red]错误[/] {len(diff_result.errors)} 个")
            for err in diff_result.errors:
                self.console.print(f"  [red]![/] {err}")

        if not has_any_output:
            self.console.print("[yellow]未检测到任何差异。[/yellow]")

    # --- 操作实现 ---

    def _load_config(self):
        """加载配置文件。"""
        try:
            self.config = load_config(self.config_path)
            errors = self.config.validate()
            if errors:
                self.console.print("[red]配置校验失败:[/red]")
                for err in errors:
                    self.console.print(f"  [red]✗[/red] {err}")
                self.config = None
                return False

            self.state = STATE_LOADED
            self.console.print(f"[green]✓[/green] 配置加载成功")
            self.console.print(f"  Base Layers: {self.config.base_layers}")
            self.console.print(f"  Target: {self.config.target}")
            self.console.print(f"  Workspace: {self.config.workspace_dir}")
            self.console.print(f"  资源类型: {self.config.resource_types}")
            return True

        except Exception as e:
            self.console.print(f"[red]配置加载失败: {e}[/red]")
            return False

    def action_mount(self):
        """[1] 挂载工作区。"""
        self.console.print("\n[bold]━━━ 挂载工作区 ━━━[/bold]\n")

        # 加载配置
        if not self._load_config():
            return

        # 检查工作区是否已存在
        ws_path = self.config.workspace_path
        if ws_path.exists():
            if not Confirm.ask(
                f"工作区已存在 ({ws_path})，是否清除并重新生成？",
                default=False,
            ):
                self.console.print("[yellow]操作取消[/yellow]")
                # 如果工作区存在，直接进入就绪状态
                self.state = STATE_WORKSPACE_READY
                return

        # 执行合并
        self.console.print()
        merge_result = merge_to_workspace(
            self.config,
            progress_callback=lambda msg: self.console.print(f"  [dim]→ {msg}[/dim]"),
        )

        if merge_result.errors:
            self.console.print("[yellow]合并过程中出现错误:[/yellow]")
            for err in merge_result.errors:
                self.console.print(f"  [yellow]![/yellow] {err}")

        self.console.print(f"\n[green]✓[/green] 工作区已生成: {ws_path}")
        self.console.print(f"  {merge_result.summary()}")
        self.console.print(
            f"\n  [cyan]现在可以用编辑器打开工作区进行编辑。[/cyan]"
            f"\n  [cyan]编辑完成后选择 [2] 刷新差异预览。[/cyan]"
        )

        self.state = STATE_WORKSPACE_READY
        self.diff_result = None

    def action_diff_preview(self):
        """[2] 刷新差异预览。"""
        self.console.print("\n[bold]━━━ 差异预览 ━━━[/bold]\n")

        if not self.config:
            self.console.print("[red]配置未加载[/red]")
            return

        self.console.print("[dim]正在计算差异...[/dim]\n")
        self.diff_result = compute_diff(self.config)

        self._render_diff_summary(self.diff_result)

        self.state = STATE_DIFF_READY
        self.console.print(
            "[cyan]确认无误后选择 [3] 执行回写，或再次 [2] 刷新。[/cyan]"
        )

    def action_write_back(self):
        """[3] 确认回写。"""
        self.console.print("\n[bold]━━━ 确认回写 ━━━[/bold]\n")

        if not self.config or not self.diff_result:
            self.console.print("[red]请先执行差异预览[/red]")
            return

        # 再次展示摘要
        self.console.print("[bold]即将写入以下变更:[/bold]\n")
        self._render_diff_summary(self.diff_result)
        self.console.print()

        target_path = self.config.target_path
        if not Confirm.ask(
            f"确认将差异回写到 [bold]{target_path}[/bold] ？",
            default=False,
        ):
            self.console.print("[yellow]操作取消[/yellow]")
            return

        # 执行回写
        write_result = write_back(
            self.config,
            self.diff_result,
            progress_callback=lambda msg: self.console.print(f"  [dim]→ {msg}[/dim]"),
        )

        if write_result.errors:
            self.console.print("[yellow]回写过程中出现错误:[/yellow]")
            for err in write_result.errors:
                self.console.print(f"  [yellow]![/yellow] {err}")

        self.console.print(f"\n[green]✓[/green] 回写完成: {write_result.summary()}")
        self.console.print(
            "\n  [cyan]请使用 Git 检查并提交覆盖包的变更。[/cyan]"
            "\n  [cyan]如需继续编辑，选择 [2] 刷新；如已完成，选择 [4] 卸载。[/cyan]"
        )

        self.state = STATE_WORKSPACE_READY
        self.diff_result = None

    def action_unmount(self):
        """[4] 卸载工作区。"""
        self.console.print("\n[bold]━━━ 卸载工作区 ━━━[/bold]\n")

        if not self.config:
            self.console.print("[red]配置未加载[/red]")
            return

        ws_path = self.config.workspace_path
        if not ws_path.exists():
            self.console.print("[yellow]工作区不存在，无需清理[/yellow]")
            self.state = STATE_IDLE
            return

        if not Confirm.ask(
            f"确认删除工作区 [bold]{ws_path}[/bold] ？\n"
            "  (请确保已完成回写操作)",
            default=False,
        ):
            self.console.print("[yellow]操作取消[/yellow]")
            return

        shutil.rmtree(ws_path)
        self.console.print(f"[green]✓[/green] 工作区已清理: {ws_path}")

        self.state = STATE_IDLE
        self.config = None
        self.diff_result = None

    def action_help(self):
        """[h] 显示使用说明。"""
        self.console.print()
        self.console.print(
            Panel(HELP_TEXT, title="使用说明", border_style="cyan", expand=False)
        )

    # --- 主循环 ---

    def run(self):
        """启动 TUI 主循环。"""
        self.console.clear()
        self.console.print(
            f"[dim]配置文件路径: {self.config_path}[/dim]\n"
        )

        # 如果配置文件不存在，提示创建
        if not self.config_path.exists():
            self.console.print(
                f"[yellow]配置文件不存在: {self.config_path}[/yellow]\n"
            )
            if Confirm.ask("是否在此位置生成示例配置文件？", default=True):
                self._create_sample_config()
            return

        # 启动时立即加载并展示配置，让开发者第一时间确认状态
        if not self._load_config():
            self.console.print(
                "\n[red]配置加载失败，请修正后重新运行。[/red]"
            )
            return

        while True:
            self.console.print()
            self._render_header()
            self.console.print()
            self._render_menu()

            available = self._get_available_actions()
            choice = Prompt.ask(
                "选择操作", choices=list(available), default="0"
            )

            if choice == "1":
                self.action_mount()
            elif choice == "2":
                self.action_diff_preview()
            elif choice == "3":
                self.action_write_back()
            elif choice == "4":
                self.action_unmount()
            elif choice == "h":
                self.action_help()
            elif choice == "0":
                self.console.print("\n[dim]再见！[/dim]")
                break

    def _create_sample_config(self):
        """生成示例配置文件。"""
        sample = {
            "workspace_dir": ".workspace",
            "target": "PC",
            "base_layers": ["base"],
            "resource_types": ["pipeline", "image", "model"],
        }

        import json

        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.config_path, "w", encoding="utf-8") as f:
            json.dump(sample, f, ensure_ascii=False, indent=4)

        self.console.print(f"[green]✓[/green] 示例配置已生成: {self.config_path}")
        self.console.print("[dim]请根据项目实际情况修改后重新运行。[/dim]")


# ============================================================
#  入口
# ============================================================


def main():
    # 确定配置文件路径
    if len(sys.argv) > 1:
        config_path = Path(sys.argv[1])
    else:
        # 优先在当前工作目录查找，其次在脚本所在目录查找
        cwd_config = Path.cwd() / "overlay_config.json"
        script_config = _SCRIPT_DIR / "overlay_config.json"

        if cwd_config.exists():
            config_path = cwd_config
        elif script_config.exists():
            config_path = script_config
        else:
            config_path = cwd_config  # 不存在时仍用 CWD 路径，后续会提示创建

    app = OverlayToolApp(config_path)
    app.run()


if __name__ == "__main__":
    main()
