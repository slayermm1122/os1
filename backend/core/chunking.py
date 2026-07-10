from __future__ import annotations


def pop_ready_speech_chunks(buffer: str) -> tuple[list[str], str]:
    chunks: list[str] = []
    sentence_endings = ".!?。！？\n"
    soft_breaks = ",;:，；、"
    min_chars = 12
    max_chars = 120

    while True:
        split_at = -1
        for index, char in enumerate(buffer):
            if index + 1 >= min_chars and char in sentence_endings:
                split_at = index + 1
                break

        if split_at == -1 and len(buffer) >= max_chars:
            candidates = [buffer.rfind(char, 0, max_chars) for char in soft_breaks + " "]
            split_at = max(candidates)
            split_at = max_chars if split_at < min_chars else split_at + 1

        if split_at == -1:
            break

        chunk = buffer[:split_at].strip()
        if chunk:
            chunks.append(chunk)
        buffer = buffer[split_at:].lstrip()

    return chunks, buffer
