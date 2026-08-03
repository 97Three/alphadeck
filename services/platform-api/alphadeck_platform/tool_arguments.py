import json
import re


def parse_arguments(raw):
    initial = raw.strip() or "{}"
    candidates = [initial]
    fenced = re.fullmatch(r"```(?:json)?\s*([\s\S]*?)\s*```", initial, re.I)
    unfenced = fenced[1].strip() if fenced else initial
    candidates.append(unfenced)
    start, end = unfenced.find("{"), unfenced.rfind("}")
    if 0 <= start < end:
        candidates.append(unfenced[start : end + 1].strip())

    def repair(value):
        output = []
        quoted, escaped = False, False
        for index, char in enumerate(value):
            if quoted:
                if escaped:
                    output.append(char)
                    escaped = False
                elif char == "\\":
                    output.append(char)
                    escaped = True
                elif char == '"':
                    output.append(char)
                    quoted = False
                elif ord(char) < 0x20:
                    output.append(json.dumps(char)[1:-1])
                else:
                    output.append(char)
            else:
                if char == '"':
                    quoted = True
                if char == "," and re.match(r"\s*[}\]]", value[index + 1 :]):
                    continue
                output.append(char)
        return "".join(output)

    for candidate in dict.fromkeys(candidates):
        for attempt in dict.fromkeys((candidate, repair(candidate))):
            try:
                value = json.loads(
                    attempt,
                    parse_constant=lambda _value: (_ for _ in ()).throw(
                        ValueError("Non-finite JSON")
                    ),
                )
                return {
                    "value": value,
                    "normalized": json.dumps(value, ensure_ascii=False, separators=(",", ":")),
                    "repaired": attempt != initial,
                }
            except ValueError:
                continue
    json.loads(initial)
    raise ValueError("Tool arguments must be valid JSON")
