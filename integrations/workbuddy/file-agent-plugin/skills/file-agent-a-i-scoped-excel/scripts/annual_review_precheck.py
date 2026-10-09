"""定向 PDF 考核区域的受限标点归一与服务端年度规则预检。"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path


# 与 apps/api/app/modules/external_extraction/scoped_schemas.py 保持同步；后端改动须同步此处。
_EXCELLENT_RE = re.compile(
    r"(?P<years>(?:(?:19|20)\d{2}\s*年?(?:度)?\s*(?:[、，,及和]\s*)?){1,12})"
    r"考核(?:结果)?\s*(?:为|：|:)?\s*优秀"
)
_YEAR_RE = re.compile(r"(?:19|20)\d{2}")


def excellent_years(text: str) -> list[int]:
    """仅从明确写有年度考核优秀的短语取年份；合格等结论不入列。"""
    return sorted({int(year.group()) for phrase in _EXCELLENT_RE.finditer(text)
                   for year in _YEAR_RE.finditer(phrase.group("years"))})


_DOT_CHARS = ".．。·・｡"
_NEVER_NORMALIZE = "-–—~～至到/／、，"
_DOT_PAIR_RE = re.compile(
    rf"(?<!\d)(?:19|20)\d{{2}}(?P<separator>[{re.escape(_DOT_CHARS)}])"
    r"(?=(?:19|20)\d{2}(?!\d))"
)
_NEVER_PAIR_RE = re.compile(
    rf"(?<!\d)(?:19|20)\d{{2}}(?P<separator>[{re.escape(_NEVER_NORMALIZE)}])"
    r"(?=(?:19|20)\d{2}(?!\d))"
)


def normalize(text: str) -> tuple[str, list[dict], list[dict]]:
    """只替换相邻四位年份之间的单个白名单点号，并报告所有黑名单连接。"""
    changes = []
    for match in _DOT_PAIR_RE.finditer(text):
        start, end = match.start(), match.end() + 4
        original = text[start:end]
        changes.append({"original": original,
                        "normalized": original[:4] + "、" + original[5:],
                        "span": [start, end]})
    warnings = []
    for match in _NEVER_PAIR_RE.finditer(text):
        start, end = match.start(), match.end() + 4
        warnings.append({"original": text[start:end], "separator": match.group("separator"),
                         "span": [start, end],
                         "reason": "区间 vs 并列含义不可由脚本确定；黑名单连接符原样保留"})
    replacements = {match.start("separator") for match in _DOT_PAIR_RE.finditer(text)}
    normalized = "".join("、" if index in replacements else char
                         for index, char in enumerate(text))
    return normalized, changes, warnings


def matched_phrases(text: str) -> list[dict]:
    """输出服务端正则实际命中的 years 片段及字符偏移。"""
    return [{"years": match.group("years"), "span": list(match.span("years"))}
            for match in _EXCELLENT_RE.finditer(text)]


def parse_expected(raw: str) -> list[int]:
    """保留用户给出的顺序，以便执行与服务端相同的列表全等比较。"""
    if raw == "":
        return []
    parts = raw.split(",")
    if any(re.fullmatch(r"(?:19|20)\d{2}", part) is None for part in parts):
        raise ValueError("--expect-years 必须是逗号分隔的四位年份，或空字符串")
    return [int(part) for part in parts]


def verify(text: str, expected: list[int], status: str | None) -> dict:
    """年份与状态都按服务端语义逐项校验。"""
    detected = excellent_years(text)
    errors = []
    if detected != expected:
        errors.append(f"年度列表不一致：期望 {expected}，实际 {detected}")
    if status == "EXCELLENT_FOUND" and not detected:
        errors.append("EXCELLENT_FOUND 要求至少一个优秀年度")
    if status in {"NO_EXCELLENT", "UNREADABLE"} and detected:
        errors.append(f"{status} 要求没有可检测的优秀年度")
    return {"detected": detected, "matches": matched_phrases(text),
            "expected": expected, "expected_status": status, "ok": not errors, "errors": errors}


def read_text(args: argparse.Namespace) -> str:
    if args.text is not None:
        return args.text
    return Path(args.text_file).read_text(encoding="utf-8-sig")


def write_normalized(args: argparse.Namespace, normalized: str) -> str | None:
    if not getattr(args, "out", None):
        return None
    output = Path(args.out)
    if args.text_file and output.resolve() == Path(args.text_file).resolve():
        raise ValueError("--out 不能覆盖原始 OCR 文本文件")
    output.write_text(normalized, encoding="utf-8")
    return str(output)


def print_result(result: dict, *, json_mode: bool) -> None:
    if json_mode:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    print(f"模式: {result['mode']}")
    for change in result.get("changes", []):
        print(f"原始片段 → 归一后片段 [{change['span'][0]}:{change['span'][1]}]: "
              f"{change['original']} → {change['normalized']}")
    for warning in result.get("warnings", []):
        print(f"WARN [{warning['span'][0]}:{warning['span'][1]}]: "
              f"{warning['original']} 原样保留；{warning['reason']}")
    if "before_detected" in result:
        print(f"归一前 detected: {result['before_detected']}")
        print(f"归一后 detected: {result['after_detected']}")
    if "detected" in result:
        print(f"detected: {result['detected']}")
    for match in result.get("matches", []):
        print(f"命中 years [{match['span'][0]}:{match['span'][1]}]: {match['years']}")
    if "normalized_text" in result:
        print(f"归一后文本:\n{result['normalized_text']}")
    if result.get("output_file"):
        print(f"已写入: {result['output_file']}")
    if "tests_passed" in result:
        print(f"self-test: {result['tests_passed']} passed")
    for error in result.get("errors", []):
        print(f"FAIL: {error}")
    if result.get("ok") and result["mode"] != "normalize":
        print("PASS")


def self_test() -> dict:
    """覆盖服务端现有 7 个实测行为及白、黑名单边界。"""
    cases = [
        ("2022、2024年度考核优秀；", [2022, 2024], [2022, 2024], 0),
        ("2022.2024年度考核优秀；", [2024], [2022, 2024], 1),
        ("2022．2024年度考核优秀；", [2024], [2022, 2024], 1),
        ("2022,2024年度考核优秀；", [2022, 2024], [2022, 2024], 0),
        ("2022 2024年度考核优秀；", [2022, 2024], [2022, 2024], 0),
        ("2022-2024年度考核优秀；", [2024], [2024], 0),
        ("2022至2024年度考核优秀；", [2024], [2024], 0),
        ("2022。2024年度考核优秀；", [2024], [2022, 2024], 1),
        ("2022·2024年度考核优秀；", [2024], [2022, 2024], 1),
        ("2022・2024年度考核优秀；", [2024], [2022, 2024], 1),
        ("2022｡2024年度考核优秀；", [2024], [2022, 2024], 1),
        ("2022—2024年度考核优秀；", [2024], [2024], 0),
        ("2022／2024年度考核优秀；", [2024], [2024], 0),
    ]
    errors = []
    for source, before, after, changed in cases:
        normalized, changes, _ = normalize(source)
        if excellent_years(source) != before or excellent_years(normalized) != after or len(changes) != changed:
            errors.append(f"用例不符: {source}")
    untouched = "2022..2024年度考核优秀；2022年.2024年度考核优秀；"
    if normalize(untouched)[0] != untouched:
        errors.append("多字符或带年字的连接不得归一")
    blacklisted = "2022-2024年度考核优秀；2022至2024年度考核优秀；"
    normalized, changes, warnings = normalize(blacklisted)
    if normalized != blacklisted or changes or len(warnings) != 2:
        errors.append("区间黑名单必须保留原文并逐条告警")
    return {"mode": "self-test", "tests_passed": len(cases) + 2 - len(errors),
            "total": len(cases) + 2, "ok": not errors, "errors": errors}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("normalize", "verify", "all"):
        command = commands.add_parser(name)
        source = command.add_mutually_exclusive_group(required=True)
        source.add_argument("--text-file", help="UTF-8 OCR 文本文件")
        source.add_argument("--text", help="直接提供 OCR 文本")
        if name == "normalize":
            command.add_argument("--out", help="复核后写入归一文本，不覆盖原始文件")
        else:
            command.add_argument("--expect-years", required=True,
                                 help="逗号分隔且升序去重的年度；无优秀年度时传空字符串")
            command.add_argument("--expect-status",
                                 choices=("EXCELLENT_FOUND", "NO_EXCELLENT", "UNREADABLE"))
        command.add_argument("--json", action="store_true")
    commands.add_parser("self-test").add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "self-test":
            result = self_test()
        else:
            source_text = read_text(args)
            if args.command == "verify":
                result = {"mode": "verify", **verify(source_text,
                          parse_expected(args.expect_years), args.expect_status)}
            else:
                normalized, changes, warnings = normalize(source_text)
                result = {"mode": args.command, "changes": changes, "warnings": warnings,
                          "normalized_text": normalized, "ok": True}
                if args.command == "normalize":
                    result["output_file"] = write_normalized(args, normalized)
                else:
                    check = verify(normalized, parse_expected(args.expect_years), args.expect_status)
                    result.update(check)
                    result["before_detected"] = excellent_years(source_text)
                    result["after_detected"] = check["detected"]
        print_result(result, json_mode=args.json)
        return 0 if result["ok"] else 1
    except (OSError, UnicodeError, ValueError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
