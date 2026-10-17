"""Rebuild text lines from positioned words.

Both PDF text layers and OCR output are converted to `Word`s, then grouped into
`Line`s by vertical position. Within a line, a wide horizontal gap starts a new
`Segment` (a table cell or a separate column), and each segment keeps its x-range
so the parser can look for a value directly below a label.
"""

from collections.abc import Iterable
from dataclasses import dataclass, field

SEGMENT_SEPARATOR = "   "
_COLUMN_GAP_CHARS = 2.0  # gap wider than this many average character widths -> new segment
_SAME_LINE_TOLERANCE = 0.5  # fraction of word height


@dataclass(frozen=True)
class Word:
    x0: float
    y0: float
    x1: float
    y1: float
    text: str

    @property
    def y_mid(self) -> float:
        return (self.y0 + self.y1) / 2

    @property
    def height(self) -> float:
        return self.y1 - self.y0


@dataclass
class Segment:
    text: str
    x0: float
    x1: float

    def overlaps(self, other: "Segment") -> bool:
        return self.x0 <= other.x1 and other.x0 <= self.x1


@dataclass
class Line:
    segments: list[Segment] = field(default_factory=list)

    @property
    def text(self) -> str:
        return SEGMENT_SEPARATOR.join(s.text for s in self.segments)

    @property
    def flat(self) -> str:
        """The line with all whitespace collapsed to single spaces."""
        return " ".join(self.text.split())


def words_to_lines(words: Iterable[Word]) -> list[Line]:
    rows: list[list[Word]] = []
    for word in sorted((w for w in words if w.text.strip()), key=lambda w: (w.y_mid, w.x0)):
        if rows:
            anchor = rows[-1][0]
            if abs(word.y_mid - anchor.y_mid) <= max(word.height, anchor.height) * _SAME_LINE_TOLERANCE:
                rows[-1].append(word)
                continue
        rows.append([word])
    return [_row_to_line(sorted(row, key=lambda w: w.x0)) for row in rows]


def _row_to_line(row: list[Word]) -> Line:
    char_width = sum(w.x1 - w.x0 for w in row) / max(1, sum(len(w.text) for w in row))
    segments = [Segment(row[0].text, row[0].x0, row[0].x1)]
    for prev, word in zip(row, row[1:]):
        current = segments[-1]
        if word.x0 - prev.x1 > _COLUMN_GAP_CHARS * char_width:
            segments.append(Segment(word.text, word.x0, word.x1))
        else:
            current.text += " " + word.text
            current.x1 = word.x1
    return Line(segments)
