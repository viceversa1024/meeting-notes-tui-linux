"""Note generation module for creating markdown notes."""

from datetime import datetime
from pathlib import Path
from typing import Optional, Any
from collections import Counter
import re

from .logger import get_logger

logger = get_logger(__name__)

try:
    from .summarizer import OllamaSummarizer
    OLLAMA_AVAILABLE = True
except ImportError:
    OLLAMA_AVAILABLE = False

try:
    from .ai_summarizer import OpenAISummarizer, AnthropicSummarizer, OpenRouterSummarizer, MeetingSummary  # type: ignore
    CLOUD_AVAILABLE = True
except ImportError:
    CLOUD_AVAILABLE = False
    # Fallback MeetingSummary for type hints
    MeetingSummary = None  # type: ignore


class NoteMaker:
    """Generate markdown meeting notes."""
    
    def __init__(
        self, 
        output_dir: str = "notes",
        transcripts_dir: str = "transcripts",
        ai_provider: str = "none",  # "cloud", "local", or "none"
        ai_model: str = "balanced",  # For cloud: tier, for local: ollama model
        api_key: Optional[str] = None,
        obsidian_dir: str = "",
        context_hint: str = ""
    ):
        """
        Initialize note maker.

        Args:
            output_dir: Directory to save notes
            transcripts_dir: Directory to save transcripts
            ai_provider: AI provider - "cloud" (OpenRouter), "local" (Ollama), or "none"
            ai_model: Model to use (tier for cloud, model name for local)
            api_key: API key for cloud provider (or use env var)
            obsidian_dir: Optional Obsidian vault folder that gets a copy of
                each summary note. Empty disables the export. Created on
                demand at save time, not here — a bad path must never block
                note creation.
            context_hint: Free-text background about the user, passed to
                the summarizer so it can fix mis-transcribed proper nouns
                (see AppConfig.context_hint)
        """
        logger.info(f"Initializing NoteMaker (output_dir: {output_dir}, transcripts_dir: {transcripts_dir}, ai_provider: {ai_provider}, ai_model: {ai_model})")
        self.output_dir = Path(output_dir).expanduser()
        self.transcripts_dir = Path(transcripts_dir).expanduser()
        self.obsidian_dir = Path(obsidian_dir).expanduser() if obsidian_dir else None
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.transcripts_dir.mkdir(parents=True, exist_ok=True)
        self.ai_provider = ai_provider
        self.summarizer: Optional[Any] = None
        self.model_label = ""  # human-readable summarizer model name
        
        if ai_provider in ["openai", "anthropic", "openrouter"]:
            if not CLOUD_AVAILABLE:
                logger.warning("Cloud AI packages not installed")
                self.ai_provider = "none"
            else:
                try:
                    from .ai_summarizer import OpenAISummarizer, AnthropicSummarizer, OpenRouterSummarizer  # type: ignore
                    
                    if ai_provider == "openai":
                        self.summarizer = OpenAISummarizer(api_key=api_key, model=ai_model, context_hint=context_hint)
                        model_name = OpenAISummarizer.MODELS[ai_model]["name"]
                        self.model_label = model_name
                        logger.info(f"AI summarization enabled (OpenAI: {model_name})")
                    elif ai_provider == "anthropic":
                        self.summarizer = AnthropicSummarizer(api_key=api_key, model=ai_model, context_hint=context_hint)
                        model_name = AnthropicSummarizer.MODELS[ai_model]["name"]
                        self.model_label = model_name
                        logger.info(f"AI summarization enabled (Anthropic: {model_name})")
                    elif ai_provider == "openrouter":
                        self.summarizer = OpenRouterSummarizer(api_key=api_key, model=ai_model, context_hint=context_hint)
                        model_name = OpenRouterSummarizer.MODELS[ai_model]["name"]
                        self.model_label = model_name
                        logger.info(f"AI summarization enabled (OpenRouter: {model_name})")
                        
                except Exception as e:
                    logger.error(f"Could not initialize cloud AI: {e}", exc_info=True)
                    self.ai_provider = "none"
                    
        elif ai_provider == "local":
            if not OLLAMA_AVAILABLE:
                logger.warning("Ollama not available")
                self.ai_provider = "none"
            else:
                try:
                    from .summarizer import OllamaSummarizer  # type: ignore
                    # Defensive fallback: empty model name reaches
                    # `ollama run "" <prompt>` and errors with "model is required".
                    self.summarizer = OllamaSummarizer(model=ai_model or "llama3.2:3b", context_hint=context_hint)
                    self.model_label = f"Ollama {ai_model or 'llama3.2:3b'}"
                    logger.info(f"AI summarization enabled (Local Ollama: {ai_model})")
                except Exception as e:
                    logger.error(f"Could not initialize Ollama: {e}", exc_info=True)
                    self.ai_provider = "none"
    
    def create_note(
        self,
        transcript_text: str,
        formatted_transcript: str,
        duration: float,
        title: Optional[str] = None,
        metadata: Optional[dict] = None,
        user_notes: str = ""
    ) -> tuple[str, str, Optional[str]]:
        """Create a markdown note and separate transcript file.
        
        Args:
            transcript_text: Plain text transcript
            formatted_transcript: Transcript with timestamps
            duration: Recording duration in seconds
            title: Optional meeting title
            metadata: Optional additional metadata
            user_notes: Optional notes written by user during recording
            
        Returns:
            Tuple of (note_path, transcript_path, error_message). error_message is None if no error occurred.
        """
        now = datetime.now()
        
        if title is None:
            title = f"Meeting {now.strftime('%Y-%m-%d %H:%M')}"
        
        logger.info(f"Creating note: {title}")
        logger.debug(f"Transcript length: {len(transcript_text.split())} words")
        
        # Generate AI summary if enabled, otherwise use simple summary
        ai_error = None
        if self.ai_provider != "none" and self.summarizer:
            try:
                if self.ai_provider in ["openai", "anthropic", "openrouter"]:
                    logger.info("Generating AI summary with cloud API")
                else:
                    logger.info("Generating AI summary with local Ollama")
                    
                ai_summary = self.summarizer.summarize(transcript_text, user_notes=user_notes)
                summary = {
                    'word_count': len(transcript_text.split()),
                    'ai_summary': ai_summary,
                    'keywords': [],  # AI summary replaces keywords
                    'questions': []  # AI summary includes this
                }
                logger.info("AI summary generated successfully")
            except Exception as e:
                ai_error = f"AI summarization failed: {type(e).__name__}: {str(e)}"
                logger.error(ai_error, exc_info=True)
                summary = self._extract_simple_summary(transcript_text)
        else:
            logger.info("Using simple keyword-based summary (AI disabled)")
            summary = self._extract_simple_summary(transcript_text)
        
        # Generate filename base (same for both files)
        safe_title = re.sub(r'[^\w\s-]', '', title.lower())
        safe_title = re.sub(r'[-\s]+', '-', safe_title)
        timestamp = now.strftime("%Y-%m-%d-%H%M%S")
        filename_base = f"{timestamp}-{safe_title[:50]}"
        
        # Get recording filename from metadata
        recording_file = metadata.get('recording_file', '') if metadata else ''
        
        # Create transcript file (plain text)
        transcript_filename = f"{filename_base}.txt"
        transcript_path = self.transcripts_dir / transcript_filename
        transcript_content = self._generate_transcript_file(
            title=title,
            date=now,
            duration=duration,
            formatted_transcript=formatted_transcript,
            recording_file=recording_file
        )
        transcript_path.write_text(transcript_content)
        logger.info(f"Transcript saved: {transcript_path}")
        
        # Create note file (markdown, summary only)
        note_filename = f"{filename_base}.md"
        note_path = self.output_dir / note_filename
        participants = self._guess_participants(
            formatted_transcript, summary.get('ai_summary')
        )
        note_content = self._generate_note_file(
            title=title,
            date=now,
            duration=duration,
            summary=summary,
            transcript_filename=transcript_filename,
            recording_file=recording_file,
            metadata=metadata or {},
            user_notes=user_notes,
            participants=participants
        )
        note_path.write_text(note_content)
        logger.info(f"Note saved: {note_path}")

        self._export_to_obsidian(note_filename, note_content)

        return str(note_path), str(transcript_path), ai_error

    def _export_to_obsidian(self, note_filename: str, note_content: str) -> None:
        """Best-effort copy of the summary note into the Obsidian vault.

        Never raises: a missing/unwritable vault must not lose the meeting
        note, which is already safely in output_dir.
        """
        if self.obsidian_dir is None:
            return
        try:
            self.obsidian_dir.mkdir(parents=True, exist_ok=True)
            vault_path = self.obsidian_dir / note_filename
            vault_path.write_text(note_content)
            logger.info(f"Note copied to Obsidian vault: {vault_path}")
        except Exception as exc:  # noqa: BLE001 - export is strictly best-effort
            logger.error(f"Obsidian export failed ({self.obsidian_dir}): {exc}")
    
    def _extract_simple_summary(self, text: str) -> dict:
        """Extract basic summary information without LLM.
        
        This is a fallback method when no AI provider is configured.
        """
        words = text.split()
        word_count = len(words)
        
        # Extract common keywords (simple frequency analysis)
        # Remove common words
        stop_words = {'the', 'a', 'an', 'and', 'or', 'but', 'in', 'on', 'at', 'to', 'for',
                     'of', 'with', 'is', 'was', 'are', 'were', 'been', 'be', 'have', 'has',
                     'had', 'do', 'does', 'did', 'will', 'would', 'could', 'should', 'may',
                     'might', 'can', 'this', 'that', 'these', 'those', 'i', 'you', 'he',
                     'she', 'it', 'we', 'they', 'my', 'your', 'his', 'her', 'its', 'our',
                     'their', 'what', 'which', 'who', 'when', 'where', 'why', 'how'}
        
        # Clean and filter words
        clean_words = [
            word.lower().strip('.,!?;:()[]{}""\'')
            for word in words
            if len(word) > 3
        ]
        
        filtered_words = [
            word for word in clean_words
            if word not in stop_words and word.isalpha()
        ]
        
        # Get top keywords
        word_freq = Counter(filtered_words)
        top_keywords = [word for word, count in word_freq.most_common(10)]
        
        # Extract sentences with question marks (potential questions)
        sentences = re.split(r'[.!?]+', text)
        questions = [s.strip() for s in sentences if '?' in s and len(s.strip()) > 10]
        
        return {
            'word_count': word_count,
            'keywords': top_keywords[:5],  # Top 5 keywords
            'questions': questions[:3]  # Up to 3 questions
        }
    
    def _generate_transcript_file(
        self,
        title: str,
        date: datetime,
        duration: float,
        formatted_transcript: str,
        recording_file: str
    ) -> str:
        """Generate plain text transcript file."""
        duration_str = self._format_duration(duration)
        date_str = date.strftime("%B %d, %Y at %I:%M %p")
        
        content = f"""Meeting: {title}
Date: {date_str}
Duration: {duration_str}
Recording: {recording_file}

{'─' * 60}

{formatted_transcript}
"""
        return content
    
    @staticmethod
    def _guess_participants(formatted_transcript: str, ai_summary: Any = None) -> list:
        """Best-guess participant names.

        Named (non-letter) transcript speakers are high confidence — they
        come from voice tags. The AI summary's participant list fills in
        people who were mentioned but not voice-tagged; placeholder junk
        (bracketed text, "unable to identify") is dropped.
        """
        names = []
        for m in re.finditer(r"\*\*\[[\d:]+\] ([^:*]+):\*\*", formatted_transcript):
            name = m.group(1).strip()
            if len(name) > 1 and name not in names:
                names.append(name)
        for p in (getattr(ai_summary, "participants", None) or []):
            p = p.strip()
            if p and len(p) > 1 and "[" not in p and "unable" not in p.lower() and p not in names:
                names.append(p)
        return names

    def _generate_note_file(
        self,
        title: str,
        date: datetime,
        duration: float,
        summary: dict,
        transcript_filename: str,
        recording_file: str,
        metadata: dict,
        user_notes: str = "",
        participants: Optional[list] = None
    ) -> str:
        """Generate markdown note file (summary only, no transcript)."""
        import json

        duration_str = self._format_duration(duration)
        date_str = date.strftime("%B %d, %Y at %I:%M %p")

        # Optional provenance lines — only present when known, so the
        # Obsidian properties panel stays free of empty fields.
        extra_lines = []
        if participants:
            extra_lines.append(f"participants: {json.dumps(participants)}")
        transcription_model = metadata.get("transcription_model", "")
        if transcription_model:
            extra_lines.append(f'transcription_model: "{transcription_model}"')
        if self.model_label:
            extra_lines.append(f'summary_model: "{self.model_label}"')
        extra = ("\n" + "\n".join(extra_lines)) if extra_lines else ""

        # Build frontmatter with transcript reference
        frontmatter = f"""---
title: "{title}"
date: {date.strftime("%Y-%m-%d")}
time: "{date.strftime("%H:%M")}"
duration_seconds: {int(duration)}
word_count: {summary['word_count']}
tags: [meeting, auto-generated]{extra}
recording_file: "{recording_file}"
transcript_file: "{transcript_filename}"
---
"""
        
        # Build content with AI summary if available (NO transcript)
        if 'ai_summary' in summary:
            # AI-powered summary
            ai = summary['ai_summary']
            summary_section = self._format_ai_summary(ai)
        else:
            # Simple keyword-based summary
            summary_section = f"""## Summary

This meeting covered several topics. Key themes included: {', '.join(summary['keywords'][:3]) if summary['keywords'] else 'various discussions'}.

### Key Topics

{self._format_list(summary['keywords']) if summary['keywords'] else '- (Auto-summary will be added when AI is enabled)'}

### Questions Raised

{self._format_list(summary['questions']) if summary['questions'] else '- None detected'}

"""
        
        # Add user notes section if present
        user_notes_section = ""
        if user_notes:
            user_notes_section = f"""## User Notes

{user_notes}

"""
        
        content = f"""{frontmatter}
# {title}

**Date:** {date_str}  
**Duration:** {duration_str}  
**Words:** {summary['word_count']:,}

{user_notes_section}{summary_section}

---

*View full transcript: Press 't' to view transcript*  
*Generated by Meeting Notes v0.3.0 on {datetime.now().strftime("%Y-%m-%d at %H:%M:%S")}*
"""
        
        return content
    
    @staticmethod
    def _format_duration(seconds: float) -> str:
        """Format duration in human-readable format."""
        hours = int(seconds // 3600)
        minutes = int((seconds % 3600) // 60)
        secs = int(seconds % 60)
        
        parts = []
        if hours > 0:
            parts.append(f"{hours} hour{'s' if hours != 1 else ''}")
        if minutes > 0:
            parts.append(f"{minutes} minute{'s' if minutes != 1 else ''}")
        if secs > 0 or not parts:
            parts.append(f"{secs} second{'s' if secs != 1 else ''}")
        
        return ", ".join(parts)
    
    @staticmethod
    def _format_list(items: list) -> str:
        """Format a list as markdown bullet points."""
        if not items:
            return "- None"
        return "\n".join(f"- {item}" for item in items)
    
    @staticmethod
    def _format_ai_summary(ai_summary: Any) -> str:
        """Format AI-generated summary as markdown."""
        sections = []
        
        # Overview
        sections.append("## AI Summary\n")
        sections.append(ai_summary.overview)
        sections.append("")
        
        # Key Points
        sections.append("### Key Points\n")
        if ai_summary.key_points:
            sections.append("\n".join(f"- {point}" for point in ai_summary.key_points))
        else:
            sections.append("- None identified")
        sections.append("")
        
        # Action Items
        sections.append("### Action Items\n")
        if ai_summary.action_items:
            sections.append("\n".join(f"- {item}" for item in ai_summary.action_items))
        else:
            sections.append("- None identified")
        sections.append("")
        
        # Decisions
        sections.append("### Decisions Made\n")
        if ai_summary.decisions:
            sections.append("\n".join(f"- {decision}" for decision in ai_summary.decisions))
        else:
            sections.append("- None identified")
        sections.append("")
        
        # Participants
        if ai_summary.participants:
            sections.append("### Participants\n")
            sections.append(", ".join(ai_summary.participants))
            sections.append("")
        
        return "\n".join(sections)


if __name__ == "__main__":
    # Simple test
    maker = NoteMaker()
    
    test_transcript = """
    Let's start with reviewing last sprint's velocity. We completed 23 story points
    which is pretty good. I think we should focus on the API endpoints first this sprint.
    We have three main endpoints to implement: user authentication, data sync, and analytics.
    What do you think about the timeline? Can we get this done in two weeks?
    """
    
    formatted = """**[00:00]** Let's start with reviewing last sprint's velocity.
    
**[00:05]** We completed 23 story points which is pretty good.

**[00:10]** I think we should focus on the API endpoints first this sprint."""
    
    note_path, transcript_path, error = maker.create_note(
        transcript_text=test_transcript,
        formatted_transcript=formatted,
        duration=125.0,
        title="Sprint Planning"
    )
    
    print(f"Created note: {note_path}")
    print(f"Created transcript: {transcript_path}")
    if error:
        print(f"Warning: {error}")
