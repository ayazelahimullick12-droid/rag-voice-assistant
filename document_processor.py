"""
Document processor: extracts content from various formats and uses Gemini
to structure it into clean markdown for the knowledge base.

Handles: images (PNG/JPG), PDFs, DOCX, DOC, TXT, MD

Uses google-genai's client.models.generate_content — NOT an Anthropic-style
client.messages.create call. Gemini reads PDFs and images natively via
types.Part.from_bytes(...), so PDFs are sent to the model directly rather
than pre-extracted page-by-page.
"""

import asyncio
import os
import re
from pathlib import Path
from typing import Optional

from google import genai
from google.genai import types


SUPPORTED_FORMATS = {'.pdf', '.txt', '.md', '.docx', '.doc', '.png', '.jpg', '.jpeg'}
MAX_FILE_SIZE = 50 * 1024 * 1024  # 50 MB

# Text/vision generation model for document processing — separate from the
# gemini-3.1-flash-live-preview used for the voice session. Override with
# DOC_PROCESSING_MODEL if this ID 404s on your account/region.
DOC_MODEL = os.environ.get("DOC_PROCESSING_MODEL", "gemini-3.5-flash")

_FENCE_RE = re.compile(r"^```(?:markdown|md)?\s*\n(.*)\n```\s*$", re.DOTALL)


def _strip_code_fence(text: str) -> str:
    """Models sometimes wrap markdown output in a ```markdown fence despite
    being told not to — strip it if present, otherwise return unchanged."""
    m = _FENCE_RE.match(text.strip())
    return m.group(1).strip() if m else text.strip()


class DocumentProcessor:
    def __init__(self, api_key: str):
        self.client = genai.Client(api_key=api_key)
        self.model = DOC_MODEL

    def _generate_sync(self, contents: list, system_instruction: Optional[str] = None) -> str:
        """Blocking call to Gemini — always run this via asyncio.to_thread."""
        config = types.GenerateContentConfig(system_instruction=system_instruction) if system_instruction else None
        response = self.client.models.generate_content(
            model=self.model,
            contents=contents,
            config=config,
        )
        return _strip_code_fence(response.text or "")

    async def _generate(self, contents: list, system_instruction: Optional[str] = None) -> str:
        return await asyncio.to_thread(self._generate_sync, contents, system_instruction)

    # -----------------------------------------------------------------------
    async def process_upload(
        self,
        file_path: Path,
        original_filename: str,
    ) -> tuple[str, str]:
        """Process an uploaded file and return (markdown_content, suggested_filename)."""
        if not file_path.exists():
            raise ValueError(f"File not found: {file_path}")

        suffix = file_path.suffix.lower()
        if suffix not in SUPPORTED_FORMATS:
            raise ValueError(
                f"Unsupported format: {suffix}. "
                f"Supported: {', '.join(sorted(SUPPORTED_FORMATS))}"
            )

        size = file_path.stat().st_size
        if size > MAX_FILE_SIZE:
            raise ValueError(f"File too large: {size / 1024 / 1024:.1f} MB (max 50 MB)")

        if suffix in {'.md', '.txt'}:
            return await self._process_text(file_path, original_filename)
        elif suffix == '.pdf':
            return await self._process_pdf(file_path, original_filename)
        elif suffix in {'.docx', '.doc'}:
            return await self._process_docx(file_path, original_filename)
        elif suffix in {'.png', '.jpg', '.jpeg'}:
            return await self._process_image(file_path, original_filename)

    # -----------------------------------------------------------------------
    # Text files (.txt, .md)
    # -----------------------------------------------------------------------
    async def _process_text(self, file_path: Path, original_filename: str) -> tuple[str, str]:
        raw = file_path.read_text(encoding='utf-8', errors='replace')

        if file_path.suffix.lower() == '.md':
            cleaned = await self._cleanup_markdown(raw)
            return cleaned, self._suggest_filename(original_filename, '.md')

        markdown = await self._generate(
            contents=[f"Please structure this text as markdown:\n\n{raw}"],
            system_instruction=(
                "You are a documentation structuring assistant. Take the provided text "
                "and convert it to clean, well-organized markdown with appropriate headings. "
                "Preserve all information and the original language. Use ## for main "
                "sections, ### for subsections. Return ONLY the markdown, nothing else."
            ),
        )
        return markdown, self._suggest_filename(original_filename, '.md')

    # -----------------------------------------------------------------------
    # Images (.png, .jpg, .jpeg)
    # -----------------------------------------------------------------------
    async def _process_image(self, file_path: Path, original_filename: str) -> tuple[str, str]:
        raw_bytes = file_path.read_bytes()
        mime_type = self._mime_for_suffix(file_path.suffix)

        markdown = await self._generate(
            contents=[
                types.Part.from_bytes(data=raw_bytes, mime_type=mime_type),
                (
                    "Extract all text and information from this image. Structure it as "
                    "clean, well-organized markdown with appropriate headings. Preserve "
                    "all information, context, and the original language. Return ONLY "
                    "the markdown, nothing else."
                ),
            ],
        )
        return markdown, self._suggest_filename(original_filename, '.md')

    # -----------------------------------------------------------------------
    # PDFs — sent directly to Gemini, which reads text, layout, and images
    # natively. No manual page extraction needed.
    # -----------------------------------------------------------------------
    async def _process_pdf(self, file_path: Path, original_filename: str) -> tuple[str, str]:
        pdf_bytes = file_path.read_bytes()

        markdown = await self._generate(
            contents=[
                types.Part.from_bytes(data=pdf_bytes, mime_type="application/pdf"),
                (
                    "Extract all information from this PDF and structure it as clean, "
                    "well-organized markdown. Use ## for main headings, ### for "
                    "subsections. Describe any diagrams, tables, or images in the "
                    "markdown. Preserve all information and the original language. "
                    "Return ONLY markdown, nothing else."
                ),
            ],
        )
        return markdown, self._suggest_filename(original_filename, '.md')

    # -----------------------------------------------------------------------
    # DOCX / DOC
    # -----------------------------------------------------------------------
    async def _process_docx(self, file_path: Path, original_filename: str) -> tuple[str, str]:
        try:
            from docx import Document
        except ImportError:
            raise ValueError("DOCX support requires python-docx. Install: pip install python-docx")

        try:
            doc = Document(file_path)
        except Exception as e:
            raise ValueError(f"Could not read DOCX file: {e}")

        lines = []
        for para in doc.paragraphs:
            text = para.text.strip()
            if not text:
                continue
            style = para.style.name if para.style else ""
            if "Heading 1" in style:
                lines.append(f"# {text}")
            elif "Heading 2" in style:
                lines.append(f"## {text}")
            elif "Heading 3" in style:
                lines.append(f"### {text}")
            else:
                lines.append(text)

        raw_text = "\n\n".join(lines)

        markdown = await self._generate(
            contents=[f"Please clean up and structure this text:\n\n{raw_text}"],
            system_instruction=(
                "You are a documentation structuring assistant. Take the provided text "
                "(which may already have some markdown headings) and clean it up into "
                "well-organized markdown. Improve heading hierarchy if needed. Preserve "
                "all information and the original language. Return ONLY the markdown, "
                "nothing else."
            ),
        )
        return markdown, self._suggest_filename(original_filename, '.md')

    # -----------------------------------------------------------------------
    async def _cleanup_markdown(self, raw: str) -> str:
        return await self._generate(
            contents=[f"Please clean up this markdown:\n\n{raw}"],
            system_instruction=(
                "You are a markdown cleanup assistant. Clean up the provided markdown: "
                "fix spacing around headings, ensure consistent heading levels, remove "
                "unnecessary blank lines, fix formatting issues. Preserve all content, "
                "information, and the original language. Return ONLY the cleaned markdown."
            ),
        )

    def _suggest_filename(self, original: str, suffix: str) -> str:
        base = Path(original).stem
        safe = "".join(c if c.isalnum() or c in '-_' else '_' for c in base)
        safe = safe.replace('__', '_').strip('_')
        if not safe:
            safe = "document"
        return f"{safe}{suffix}"

    @staticmethod
    def _mime_for_suffix(suffix: str) -> str:
        mapping = {
            '.png': 'image/png',
            '.jpg': 'image/jpeg',
            '.jpeg': 'image/jpeg',
        }
        return mapping.get(suffix.lower(), 'image/png')
