import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from switchmaxer.resumes import (
    ResumeError, _SkillsLayoutError, _check_variant, job_skills, prepare_resume, read_pdf, validate_source,
)


class ResumeMatchingTest(unittest.TestCase):
    def test_requires_and_prefers_skills_in_local_lists(self):
        skills = job_skills(
            "Required Skills: Java, Kubernetes, OpenShift, with Ruby a plus.\n"
            "Preferred qualifications:\nC++, JS, and Postgres.",
            "Docker | Kubernetes",
        )
        self.assertEqual(skills["Kubernetes"], 2)
        self.assertEqual(skills["OpenShift"], 2)
        self.assertEqual(skills["Java"], 2)
        self.assertEqual(skills["Ruby"], 1)
        self.assertEqual(skills["JavaScript"], 1)
        self.assertEqual(skills["PostgreSQL"], 1)
        self.assertEqual(skills["C++"], 1)
        self.assertNotIn("C", skills)
        self.assertNotIn("Docker", skills)

    def test_uses_best_match_then_default_and_only_resume(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            default_path = base / "profile.pdf"
            default_path.write_bytes(b"Default resume bytes")
            alternate_path = base / "platform.pdf"
            alternate_path.write_bytes(b"Alternate resume bytes")
            default = {"id": 1, "name": "profile.pdf", "path": default_path, "text": "Java, SQL", "is_default": True}
            alternate = {"id": 2, "name": "platform.pdf", "path": alternate_path, "text": "OpenShift, SQL"}

            preferred = prepare_resume(
                [default, alternate], "Required skills: OpenShift\nPreferred: Java", "", False, base,
            )
            self.assertEqual(preferred.resume_id, 2)
            self.assertEqual(preferred.gaps, ["Java"])
            self.assertEqual(prepare_resume([alternate, default], "SQL", "", False, base).resume_id, 1)
            self.assertEqual(prepare_resume([default], "Python", "", False, base).resume_id, 1)

    def test_rejects_source_traversal(self):
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(ResumeError, "relative"):
                validate_source(temporary, "../resume.tex", "skills.tex")


@unittest.skipUnless(shutil.which("tectonic"), "Tectonic is unavailable")
class LatexResumeTest(unittest.TestCase):
    def test_only_confirmed_skills_enter_validated_cached_variants(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary)
            source = path / "source"
            source.mkdir()
            (source / "resume.tex").write_text(
                r"""\documentclass[11pt,a4paper]{article}
\usepackage{unicode-math}
\usepackage[default,opentype]{sourcesanspro}
\newcommand{\cvsection}[1]{\section*{#1}}
\newenvironment{cvskills}{\noindent\begin{tabular}{ll}}{\end{tabular}}
\newcommand{\cvskill}[2]{#1 & #2\\}
\begin{document}
\section*{Example Applicant}
applicant@example.com
\input{skills.tex}
\section*{Experience}
Validated content outside skills.
\end{document}
""",
                encoding="utf-8",
            )
            original_tex = r"""\cvsection{Skills}
\begin{cvskills}
\cvskill{Languages}{Java, C++, Rust, Go, Lua, Perl}
\cvskill{Tools and Technologies}{AWS, Linux, GitHub, Docker, Perforce}
\end{cvskills}
"""
            skills_file = source / "skills.tex"
            skills_file.write_text(original_tex, encoding="utf-8")
            initial = subprocess.run(
                ["tectonic", "--untrusted", "--only-cached", "--outdir", str(path), "resume.tex"],
                cwd=source, capture_output=True, timeout=35, check=False,
            )
            if initial.returncode:
                self.skipTest("The necessary Tectonic bundle is unavailable offline")
            original = path / "resume.pdf"
            self.assertEqual(read_pdf(original).email, "applicant@example.com")
            row = {
                "id": 1, "name": "resume.pdf", "path": original, "source_dir": source,
                "entry_file": "resume.tex", "skills_file": "skills.tex",
            }
            cache = path / "cache"
            description = "Required skills: Kubernetes, Java, Docker, COBOL"
            variant = prepare_resume([row], description, "Kubernetes", True, cache)
            self.assertTrue(variant.tailored, variant.warning)
            self.assertEqual(variant.additions, ["Kubernetes"])
            self.assertEqual(variant.removals, [])
            self.assertEqual(variant.gaps, ["COBOL"])
            info = read_pdf(variant.pdf_path)
            self.assertEqual(info.pages, 1)
            self.assertIn("Validated content outside skills.", info.text)
            self.assertIn("Docker, Kubernetes, AWS, Linux, GitHub, Perforce", " ".join(info.text.split()))
            self.assertNotIn("COBOL", info.text)
            self.assertEqual(skills_file.read_text(encoding="utf-8"), original_tex)
            self.assertEqual(prepare_resume([row], description, "Kubernetes", True, cache), variant)

            attempts = 0

            def make_room(*args):
                nonlocal attempts
                attempts += 1
                if attempts < 3:
                    raise _SkillsLayoutError("The skills need room in this test.")
                _check_variant(*args)

            with patch("switchmaxer.resumes._check_variant", side_effect=make_room):
                trimmed = prepare_resume([row], description, "Kubernetes", True, cache / "limited")
                self.assertEqual(prepare_resume([row], description, "Kubernetes", True, cache / "limited"), trimmed)
            self.assertEqual(attempts, 3)
            self.assertTrue(trimmed.tailored, trimmed.warning)
            self.assertEqual(trimmed.additions, ["Kubernetes"])
            self.assertEqual(len(trimmed.removals), 2)
            self.assertLessEqual(set(trimmed.removals), {"AWS", "Linux", "GitHub", "Perforce"})
            self.assertIn("Docker, Kubernetes", " ".join(read_pdf(trimmed.pdf_path).text.split()))

            with patch("switchmaxer.resumes._check_variant", side_effect=_SkillsLayoutError("A layout issue.")) as check:
                reorder = prepare_resume([row], "Required skills: Docker", "", True, cache / "reorder")
            self.assertEqual(check.call_count, 1)
            self.assertFalse(reorder.tailored)
            self.assertEqual(reorder.removals, [])

            long_skill = "W" * 85
            invalid = prepare_resume([row], f"Required skills: {long_skill}", long_skill, True, cache)
            self.assertFalse(invalid.tailored)
            self.assertEqual(Path(invalid.pdf_path), original.resolve())
            self.assertIn("original PDF is ready", invalid.warning)


if __name__ == "__main__":
    unittest.main()
