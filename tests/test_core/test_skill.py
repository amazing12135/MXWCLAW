"""测试 core/skill.py"""

from pathlib import Path

from mxwbot.core.skill import Skill, SkillLoader


_SAMPLE_SKILL = """---
name: test-skill
description: A test skill
availability: always
dependencies: [python]
---

# Test Skill

This is a test skill body.
"""


class TestSkillLoader:
    def test_scan_single_file(self, tmp_path: Path):
        skill_dir = tmp_path / "skills"
        skill_dir.mkdir()
        (skill_dir / "test.md").write_text(_SAMPLE_SKILL)

        loader = SkillLoader(skill_dir)
        skills = loader.scan()
        assert len(skills) == 1
        assert skills[0].name == "test-skill"
        assert skills[0].availability == "always"
        assert "test skill body" in skills[0].content

    def test_skill_always_loaded(self, tmp_path: Path):
        skill_dir = tmp_path / "skills"
        skill_dir.mkdir()
        (skill_dir / "manual.md").write_text(
            "---\nname: m1\ndescription: d\navailability: manual\n---\nbody"
        )
        (skill_dir / "always.md").write_text(
            "---\nname: a1\ndescription: d\navailability: always\n---\nbody"
        )
        loader = SkillLoader(skill_dir)
        loader.scan()
        always = loader.get_always_loaded()
        assert len(always) == 1
        assert always[0].name == "a1"

    def test_to_context_xml(self, tmp_path: Path):
        skill_dir = tmp_path / "skills"
        skill_dir.mkdir()
        (skill_dir / "s1.md").write_text(_SAMPLE_SKILL)
        loader = SkillLoader(skill_dir)
        loader.scan()
        xml = loader.to_context_xml()
        assert "<skills>" in xml
        assert "test-skill" in xml

    def test_skill_to_xml(self):
        skill = Skill(name="code", description="审查代码", path=Path("/tmp"))
        xml = skill.to_xml()
        assert '<skill name="code"' in xml
        assert "审查代码" in xml

    def test_get_nonexistent(self, tmp_path: Path):
        loader = SkillLoader(tmp_path)
        assert loader.get("nonexistent") is None

    def test_missing_frontmatter_skipped(self, tmp_path: Path):
        skill_dir = tmp_path / "skills"
        skill_dir.mkdir()
        (skill_dir / "bad.md").write_text("no frontmatter here")
        loader = SkillLoader(skill_dir)
        skills = loader.scan()
        assert len(skills) == 0
