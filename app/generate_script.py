import os
import sys
import json
import re
import argparse
from typing import Optional
from default_prompts import DEFAULT_SYSTEM_PROMPT, DEFAULT_USER_PROMPT
from utils import (
    clean_json_string, repair_json_array, salvage_json_entries,
    load_llm_config, load_generation_config, load_prompts_config,
    create_llm_client, log_llm_response, resolve_task_llm,
    PARA_MARKER, CHAP_MARKER,
)

# Cap for single-speaker mode: entries at this size pass through
# group_into_chunks (MAX_CHUNK_CHARS=500) as-is without further splitting.
SINGLE_SPEAKER_MAX_CHARS = 500



def fix_mojibake(text):
    """Fix common mojibake characters resulting from CP1252-as-UTF8."""
    replacements = {
        'â€™': ''',  # Right single quote
        'â€˜': ''',  # Left single quote
        'â€œ': '"',  # Left double quote
        'â€\x9d': '"', # Right double quote
        'â€?': '"', # Sometimes ? if undefined
        'â€"': '—',  # Em dash
        'â€"': '–',  # En dash
        'â€¦': '…',  # Ellipsis
    }

    for bad, good in replacements.items():
        text = text.replace(bad, good)

    return text

def split_into_chunks(text, max_size=3000):
    """Split text into chunks at paragraph boundaries, never crossing chapters.

    The flattened EPUB text carries two structural markers (see utils.py):
    PARA_MARKER (``<[para]>``) is a soft boundary - consecutive paragraphs merge
    into a chunk up to ``max_size``. CHAP_MARKER (``<[chap]>``) is a hard
    boundary - a chunk never spans two chapters. Both markers are stripped, so
    chunk text is clean prose. Oversized paragraphs (> ``max_size``) fall back to
    splitting at sentence boundaries.
    """
    chunks = []

    # Hard-split on chapter markers first so a chunk never crosses a chapter seam.
    for chapter in re.split(re.escape(CHAP_MARKER), text):
        current_chunk = ""

        # Soft-split on paragraph markers; consecutive paragraphs merge below max_size.
        for para in re.split(re.escape(PARA_MARKER), chapter):
            para = para.strip()
            if not para:
                continue

            if len(current_chunk) + len(para) + 2 > max_size:
                if current_chunk:
                    chunks.append(current_chunk.strip())
                    current_chunk = ""

                if len(para) > max_size:
                    sentences = re.split(r'(?<=[.!?])\s+', para)
                    for sentence in sentences:
                        if len(current_chunk) + len(sentence) + 1 > max_size:
                            if current_chunk:
                                chunks.append(current_chunk.strip())
                            current_chunk = sentence
                        else:
                            current_chunk += " " + sentence if current_chunk else sentence
                else:
                    current_chunk = para
            else:
                current_chunk += "\n\n" + para if current_chunk else para

        if current_chunk:
            chunks.append(current_chunk.strip())

    return chunks

def process_chunk(client, model_name, chunk, chunk_num, total_chunks, previous_entries=None, max_retries=2, system_prompt=None, user_prompt_template=None, max_tokens=4096, temperature=0.6, top_p=0.8, top_k=0, min_p=0, presence_penalty=0.0, banned_tokens=None, reasoning_effort: Optional[str] = None):
    """Send a text chunk to the LLM and return parsed script entries.

    Builds a prompt that includes positional context (beginning/middle/end),
    a character roster from previous chunks for name consistency, and the
    last few entries for style continuity. Retries on failure up to
    ``max_retries`` times, attempting JSON repair on malformed responses.

    Args:
        client: OpenAI-compatible chat completion client.
        model_name: Model identifier to pass to the API.
        chunk: Raw text of the chunk to process.
        chunk_num: 1-based index of this chunk within the full text.
        total_chunks: Total number of chunks the source text was split into.
        previous_entries: Script entries from earlier chunks, used to build
            character roster and style continuity context.
        max_retries: Number of retry attempts on API or parse failure.
        system_prompt: Override for the system prompt. Falls back to
            ``DEFAULT_SYSTEM_PROMPT`` when ``None``.
        user_prompt_template: Override for the user prompt template. Must
            contain ``{context}`` and ``{chunk}`` placeholders. Falls back
            to ``DEFAULT_USER_PROMPT`` when ``None``.
        max_tokens: Maximum tokens in the LLM response.
        temperature: Sampling temperature.
        top_p: Nucleus sampling threshold.
        top_k: Top-k sampling limit. ``0`` means unset (omitted from request).
        min_p: Minimum probability threshold. ``0`` means unset.
        presence_penalty: Presence penalty factor.
        banned_tokens: List of token strings the model should avoid.
        reasoning_effort: Controls the model's reasoning depth
            (e.g. ``'low'``, ``'medium'``, ``'high'`` for OpenAI-compatible
            providers). ``None`` means use the provider default. Passed via
            ``extra_body``.

    Returns:
        List of script entry dicts (each with ``speaker``, ``text``, and
        optionally ``instruct`` keys), or an empty list if all retries fail.
    """
    # Use provided prompts or fall back to defaults
    sys_prompt = system_prompt or DEFAULT_SYSTEM_PROMPT
    usr_template = user_prompt_template or DEFAULT_USER_PROMPT

    context_parts = []

    if chunk_num == 1:
        context_parts.append("(Beginning of text)")
    elif chunk_num == total_chunks:
        context_parts.append("(End of text)")
    else:
        context_parts.append(f"(Part {chunk_num} of {total_chunks})")

    if previous_entries and len(previous_entries) > 0:
        # Build character roster for name consistency across chunks
        characters_seen = sorted(set(
            entry.get("speaker", "") for entry in previous_entries
            if entry.get("speaker", "") and entry.get("speaker", "") != "NARRATOR"
        ))
        if characters_seen:
            context_parts.append(f"Characters in this book: {', '.join(characters_seen)}")

        # Include last few entries so the model can maintain style and tone continuity
        tail = previous_entries[-3:]
        context_parts.append("\nPrevious section ended with:")
        for entry in tail:
            context_parts.append(json.dumps(entry, ensure_ascii=False))

    context = "\n".join(context_parts)
    user_prompt = usr_template.format(context=context, chunk=chunk)

    for attempt in range(max_retries + 1):
        try:
            response = client.chat.completions.create(
                model=model_name,
                messages=[
                    {"role": "system", "content": sys_prompt},
                    {"role": "user", "content": user_prompt}
                ],
                temperature=temperature,
                top_p=top_p,
                presence_penalty=presence_penalty,
                max_tokens=max_tokens,
                extra_body={
                    k: v for k, v in {
                        "top_k": top_k if top_k else None,
                        "min_p": min_p if min_p else None,
                        "banned_tokens": banned_tokens if banned_tokens else None,
                        "reasoning_effort": reasoning_effort,
                    }.items() if v is not None
                }
            )

            choice = response.choices[0]
            text = choice.message.content.strip()
            finish_reason = choice.finish_reason
            usage = getattr(response, 'usage', None)

            log_llm_response(
                "llm_responses.log",
                f"CHUNK {chunk_num}/{total_chunks}",
                text,
                finish_reason=finish_reason,
                usage=usage,
                attempt=attempt + 1,
            )

            print(f"  finish_reason={finish_reason}", end="")
            if usage:
                print(f" | tokens: prompt={getattr(usage, 'prompt_tokens', '?')} completion={getattr(usage, 'completion_tokens', '?')}", end="")
            print()

            if finish_reason == "length":
                print(f"  WARNING: Response was truncated (hit max_tokens={max_tokens}). Consider increasing max_tokens.")

        except Exception as e:
            print(f"Error calling LLM API (attempt {attempt + 1}): {e}")
            if attempt < max_retries:
                continue
            return []

        # Clean and extract JSON from response
        json_text = clean_json_string(text)

        if not json_text:
            print(f"Warning: Could not find JSON array in chunk {chunk_num} response (attempt {attempt + 1})")
            if attempt < max_retries:
                print("Retrying...")
                continue
            print(f"Response preview: {text[:300]}...")
            return []

        # Try to parse, with repair attempts
        entries = repair_json_array(json_text)

        if entries and len(entries) > 0:
            if attempt > 0:
                print(f"  Succeeded on retry {attempt + 1}")
            return entries

        # If repair failed, show warning
        print(f"Warning: Could not parse chunk {chunk_num} response as JSON (attempt {attempt + 1})")
        print(f"JSON preview: {json_text[:300]}...")

        if attempt < max_retries:
            print("Retrying with lower temperature...")

        # Last resort: extract individual valid entries with regex
        salvaged_entries = salvage_json_entries(json_text)
        if salvaged_entries:
            print(f"Regex-salvaged {len(salvaged_entries)} entries from malformed response")
            return salvaged_entries

    return []

def _write_script_output(all_entries):
    """Write annotated_script.json and clear stale chunks.json."""
    output_path = os.path.join("..", "annotated_script.json")
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(all_entries, f, indent=2, ensure_ascii=False)

    chunks_path = os.path.join("..", "chunks.json")
    if os.path.exists(chunks_path):
        os.remove(chunks_path)
        print("Cleared old chunks.json")

    speakers = set(entry.get("speaker") or entry.get("type") or "UNKNOWN" for entry in all_entries)
    print(f"\nGenerated {len(all_entries)} script entries")
    print(f"Speakers found: {', '.join(sorted(speakers))}")
    print(f"Output saved to: {output_path}")


def run_single_speaker(book_content, speaker_name, instruct):
    """Bypass the LLM and emit one entry per text segment, all attributed
    to a single speaker. Used for first-person memoirs, non-fiction, etc.,
    where character detection is unnecessary."""
    segments = split_into_chunks(book_content, max_size=SINGLE_SPEAKER_MAX_CHARS)
    print(f"Split into {len(segments)} narration segments at paragraph/sentence boundaries")

    entries = [
        {"speaker": speaker_name, "text": segment, "instruct": instruct}
        for segment in segments
    ]

    if not entries:
        print("Error: No script entries generated (input text is empty?)")
        sys.exit(1)

    _write_script_output(entries)


def main():
    parser = argparse.ArgumentParser(description="Generate annotated audiobook script.")
    parser.add_argument("input_file_path", help="Path to the input text/markdown/EPUB text file.")
    parser.add_argument("--single-speaker", action="store_true",
                        help="Skip LLM and attribute the whole text to one speaker.")
    parser.add_argument("--speaker-name", default="Narrator",
                        help="Speaker name used in single-speaker mode (default: Narrator).")
    parser.add_argument("--instruct", default="Neutral narration.",
                        help="Voice direction used in single-speaker mode.")
    args = parser.parse_args()

    input_file_path = args.input_file_path
    print(f"Processing book from: {input_file_path}")

    if not os.path.exists(input_file_path):
        print(f"Error: Input file not found: {input_file_path}")
        sys.exit(1)

    with open(input_file_path, 'r', encoding='utf-8') as f:
        book_content = f.read()

    # Fix encoding artifacts
    book_content = fix_mojibake(book_content)

    print(f"Read {len(book_content)} characters")

    if args.single_speaker:
        print(f"Single-speaker mode: attributing all narration to '{args.speaker_name}'")
        run_single_speaker(book_content, args.speaker_name, args.instruct)
        return

    # Resolve per-task LLM config
    resolved = resolve_task_llm("script_generation")
    model_name = resolved["model_name"]
    reasoning_effort = resolved["reasoning_effort"]

    # Load LLM config for base_url (client encapsulates api_key/base_url)
    llm = load_llm_config()
    base_url = llm["base_url"]

    # Load custom prompts or use defaults
    prompts_config = load_prompts_config()
    system_prompt = prompts_config.get("system_prompt") or DEFAULT_SYSTEM_PROMPT
    user_prompt_template = prompts_config.get("user_prompt") or DEFAULT_USER_PROMPT

    # Load generation settings
    generation_config = load_generation_config()
    chunk_size = generation_config.get("chunk_size", 3000)
    max_tokens = generation_config.get("max_tokens", 4096)
    temperature = generation_config.get("temperature", 0.6)
    top_p = generation_config.get("top_p", 0.8)
    top_k = generation_config.get("top_k", 0)
    min_p = generation_config.get("min_p", 0)
    presence_penalty = generation_config.get("presence_penalty", 0.0)
    banned_tokens = generation_config.get("banned_tokens", [])

    print(f"Connecting to: {base_url}")
    print(f"Using model: {model_name}")
    print(f"Chunk size: {chunk_size} chars, Max tokens: {max_tokens}")
    if banned_tokens:
        print(f"Banned tokens: {banned_tokens}")

    # Create OpenAI client
    client, _ = create_llm_client()

    # Split into chunks at natural boundaries
    chunks = split_into_chunks(book_content, max_size=chunk_size)
    total_chunks = len(chunks)

    print(f"Split into {total_chunks} chunks at paragraph/sentence boundaries")

    all_entries = []
    for i, chunk in enumerate(chunks, 1):
        print(f"Processing chunk {i}/{total_chunks} ({len(chunk)} chars)...")

        previous = all_entries if len(all_entries) > 0 else None
        entries = process_chunk(
            client, model_name, chunk, i, total_chunks,
            previous_entries=previous,
            system_prompt=system_prompt,
            user_prompt_template=user_prompt_template,
            max_tokens=max_tokens,
            temperature=temperature,
            top_p=top_p,
            top_k=top_k,
            min_p=min_p,
            presence_penalty=presence_penalty,
            banned_tokens=banned_tokens,
            reasoning_effort=reasoning_effort,
        )
        all_entries.extend(entries)
        print(f"  Got {len(entries)} entries")

    if not all_entries:
        print("Error: No script entries generated")
        sys.exit(1)

    _write_script_output(all_entries)


if __name__ == '__main__':
    main()
