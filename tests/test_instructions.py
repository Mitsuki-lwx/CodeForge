"""项目指令文件与 @include 展开器单测。"""

from __future__ import annotations

from pathlib import Path

from core.instructions.discovery import discover_instruction_files
from core.instructions.include import MAX_INCLUDE_DEPTH, load_instructions


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


# ── 发现与优先级 ──────────────────────────────────────────────────


def test_discover_three_levels(tmp_path, monkeypatch):
    proj = tmp_path / "proj"
    (proj / ".codeforge").mkdir(parents=True)
    monkeypatch.setattr(
        "core.instructions.discovery.Path.home",
        lambda: tmp_path / "home",
    )
    _write(proj / "CODEFORGE.md", "# root")
    _write(proj / ".codeforge" / "CODEFORGE.md", "# config")
    _write(tmp_path / "home" / ".codeforge" / "CODEFORGE.md", "# user")

    files = discover_instruction_files(proj)
    assert [f.path.name for f in files] == [
        "CODEFORGE.md",
        "CODEFORGE.md",
        "CODEFORGE.md",
    ]
    # 项目根在最前，用户级在最后
    assert files[0].path.parent == proj
    assert files[1].path.parent == proj / ".codeforge"
    assert files[2].path.parent == tmp_path / "home" / ".codeforge"
    # 沙箱根：前两个是项目根，用户级是 ~/.codeforge/
    assert files[0].root == proj.resolve()
    assert files[2].root == (tmp_path / "home" / ".codeforge").resolve()


def test_discover_missing_skipped(tmp_path, monkeypatch):
    proj = tmp_path / "proj"
    (proj / ".codeforge").mkdir(parents=True)
    monkeypatch.setattr(
        "core.instructions.discovery.Path.home",
        lambda: tmp_path / "home",
    )
    _write(proj / "CODEFORGE.md", "# only root")
    files = discover_instruction_files(proj)
    assert len(files) == 1
    assert files[0].path == proj / "CODEFORGE.md"


def test_load_instructions_priority_order(tmp_path, monkeypatch):
    proj = tmp_path / "proj"
    monkeypatch.setattr(
        "core.instructions.discovery.Path.home",
        lambda: tmp_path / "home",
    )
    _write(proj / "CODEFORGE.md", "ROOT_CONTENT")
    _write(proj / ".codeforge" / "CODEFORGE.md", "CONFIG_CONTENT")
    _write(tmp_path / "home" / ".codeforge" / "CODEFORGE.md", "USER_CONTENT")

    text = load_instructions(proj)
    # 高优先级在前
    assert (
        text.index("ROOT_CONTENT")
        < text.index("CONFIG_CONTENT")
        < text.index("USER_CONTENT")
    )
    # 来源标注
    assert "## 来自" in text


def test_load_instructions_empty_when_no_files(tmp_path):
    assert load_instructions(tmp_path / "empty") == ""


# ── @include 展开 ─────────────────────────────────────────────────


def test_include_replaces_line(tmp_path):
    _write(tmp_path / "proj" / "CODEFORGE.md", "@include rules/style.md\nKEEP\n")
    _write(tmp_path / "proj" / "rules" / "style.md", "STYLE_CONTENT\n")
    text = load_instructions(tmp_path / "proj")
    assert "STYLE_CONTENT" in text
    assert "KEEP" in text
    assert "@include" not in text


def test_include_resolves_relative_to_current_file_dir(tmp_path):
    # CODEFORGE.md 在根，@include 相对其目录（项目根）
    _write(tmp_path / "proj" / "CODEFORGE.md", "@include sub/a.md\n")
    _write(tmp_path / "proj" / "sub" / "a.md", "A_CONTENT\n")
    text = load_instructions(tmp_path / "proj")
    assert "A_CONTENT" in text


def test_include_nested_limit(tmp_path):
    # CODEFORGE(1) → f1(2) → f2(3) → f3(4) → f4(5) → f5(6，跳过)
    _write(tmp_path / "proj" / "CODEFORGE.md", "@include f1.md\n")
    for i in range(1, 6):
        target = f"f{i + 1}.md" if i < 5 else None
        body = f"@include {target}\n" if target else "F5_CONTENT\n"
        _write(tmp_path / "proj" / f"f{i}.md", body)
    _write(tmp_path / "proj" / "f6.md", "F6_CONTENT\n")

    text = load_instructions(tmp_path / "proj")
    assert "超过最大嵌套深度" in text  # f5 处跳过的警告注释
    assert "F6_CONTENT" not in text
    assert "F5_CONTENT" not in text  # 第 6 层未被加载


def test_include_cycle_detection(tmp_path):
    _write(tmp_path / "proj" / "CODEFORGE.md", "@include a.md\n")
    _write(tmp_path / "proj" / "a.md", "@include b.md\n")
    _write(tmp_path / "proj" / "b.md", "@include a.md\nB_CONTENT\n")
    text = load_instructions(tmp_path / "proj")
    assert "检测到环路" in text
    assert "B_CONTENT" in text  # b 被加载，但 a 不再重复加载


def test_include_escape_blocked(tmp_path):
    _write(tmp_path / "proj" / "CODEFORGE.md", "@include ../../outside.md\n")
    _write(tmp_path / "outside.md", "OUTSIDE\n")
    text = load_instructions(tmp_path / "proj")
    assert "路径超出允许范围" in text
    assert "OUTSIDE" not in text


def test_include_missing_silent(tmp_path):
    _write(tmp_path / "proj" / "CODEFORGE.md", "@include missing.md\nKEEP\n")
    text = load_instructions(tmp_path / "proj")
    assert "KEEP" in text
    assert "@include" not in text  # 指令行被消费，文件缺失静默


def test_include_binary_skipped(tmp_path):
    (tmp_path / "proj").mkdir(parents=True)
    (tmp_path / "proj" / "bin.dat").write_bytes(b"\x00\x01\x02")
    _write(tmp_path / "proj" / "CODEFORGE.md", "@include bin.dat\n")
    text = load_instructions(tmp_path / "proj")
    assert "文件为二进制" in text


def test_include_inline_not_expanded(tmp_path):
    _write(tmp_path / "proj" / "CODEFORGE.md", "see @include x.md in middle\n")
    _write(tmp_path / "proj" / "x.md", "X_CONTENT\n")
    text = load_instructions(tmp_path / "proj")
    assert "@include x.md" in text  # 保持原文
    assert "X_CONTENT" not in text


def test_max_include_depth_value():
    assert MAX_INCLUDE_DEPTH == 5


# ── 拼装器注入（F43/AC27）────────────────────────────────────────


def test_builder_injection_order():
    from core.prompts.builder import PromptBuilder

    builder = PromptBuilder()
    builder.set_injections(instructions="INST_TEXT", memory="MEM_TEXT")
    assembly = builder.build_assembly()
    stable = "\n".join(b.content for b in assembly.cached)
    assert "INST_TEXT" in stable
    assert "MEM_TEXT" in stable
    assert stable.index("INST_TEXT") < stable.index("MEM_TEXT")
    # 固定模块仍在注入之前
    assert stable.index("You are CodeForge") < stable.index("INST_TEXT")


def test_builder_injection_empty_skipped():
    from core.prompts.builder import PromptBuilder

    builder = PromptBuilder(instructions="", memory="")
    assembly = builder.build_assembly()
    stable = "\n".join(b.content for b in assembly.cached)
    assert "INST_TEXT" not in stable
    assert "MEM_TEXT" not in stable
    assert "You are CodeForge" in stable  # 既有行为不变
