#!/usr/bin/env python3
import base64
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

ALLOWED_EXTENSIONS = {
    ".cs", ".cpp", ".c", ".h", ".hpp", ".shader", ".hlsl",
    ".js", ".ts", ".py", ".json", ".bat", ".cmd", ".ps1", ".sh",
    ".yml", ".yaml"
}

GH_BASE = "https://" + "api.github.com"


def clean_url(url):
    s = str(url).strip()
    if "](" in s:
        s = s.split("](")[-1].rstrip(")")
    for ch in '[]() "\'':
        s = s.replace(ch, "")
    return s.strip()


def http_request_raw(url, method="GET", headers=None, data=None):
    url = clean_url(url)
    req = urllib.request.Request(url, method=method)
    if headers:
        for k, v in headers.items():
            req.add_header(k, v)
    payload = json.dumps(data).encode("utf-8") if data is not None else None
    with urllib.request.urlopen(req, data=payload, timeout=120) as resp:
        content = resp.read().decode("utf-8")
        return json.loads(content) if content else {}, resp.headers, resp.status


def http_request(url, method="GET", headers=None, data=None):
    data, _, _ = http_request_raw(url, method=method, headers=headers, data=data)
    return data


def post_with_fallback(url, data, primary_name, primary_headers, fallback_name, fallback_headers):
    clean_target = clean_url(url)
    try:
        http_request(clean_target, method="POST", headers=primary_headers, data=data)
        return True
    except urllib.error.HTTPError as e:
        err_msg = e.read().decode("utf-8", errors="replace")
        print(f"  {primary_name} rejected POST to {clean_target} (HTTP {e.code}): {err_msg.strip()}")

        if fallback_headers:
            print(f"  Attempting fallback with {fallback_name}...")
            try:
                http_request(clean_target, method="POST", headers=fallback_headers, data=data)
                print(f"  Success: Posted using {fallback_name}!")
                return True
            except urllib.error.HTTPError as e_fb:
                err_fb = e_fb.read().decode("utf-8", errors="replace")
                print(f"  {fallback_name} also rejected POST (HTTP {e_fb.code}): {err_fb.strip()}")
            except Exception as e_fb_gen:
                print(f"  {fallback_name} unexpected failure: {e_fb_gen}")
    except Exception as e_gen:
        print(f"  Unexpected failure on POST {clean_target}: {e_gen}")

    return False


def parse_diff_hunk(patch):
    valid_lines = set()
    indent_map = {}
    line_content_map = {}
    annotated_lines = []

    if not patch:
        return valid_lines, indent_map, line_content_map, ""

    cur = 0
    for line in patch.splitlines():
        match = re.match(r"^@@ -\d+(?:,\d+)? \+(\d+)", line)
        if match:
            cur = int(match.group(1))
            annotated_lines.append(line)
        elif line.startswith("+") and not line.startswith("+++"):
            valid_lines.add(cur)
            content = line[1:]
            line_content_map[cur] = content
            indent_match = re.match(r"^(\s+)", content)
            indent_map[cur] = indent_match.group(1) if indent_match else ""
            annotated_lines.append(f"[Line {cur}] {line}")
            cur += 1
        elif line.startswith(" "):
            valid_lines.add(cur)
            content = line[1:]
            line_content_map[cur] = content
            indent_match = re.match(r"^(\s+)", content)
            indent_map[cur] = indent_match.group(1) if indent_match else ""
            annotated_lines.append(f"[Line {cur}] {line}")
            cur += 1
        else:
            annotated_lines.append(line)

    return valid_lines, indent_map, line_content_map, "\n".join(annotated_lines)


def main():
    pat_token = (os.environ.get("UNITY_FRAMEWORK_PAT") or "").strip().strip('"').strip("'")
    actions_token = (os.environ.get("ACTIONS_GITHUB_TOKEN") or os.environ.get("GITHUB_TOKEN") or "").strip()
    qwen_api_key = (os.environ.get("QWEN_API_KEY") or "").strip().strip('"').strip("'")
    qwen_api_base = (
            os.environ.get("QWEN_API_BASE")
            or "http://" + "ai-research-mt.schellgames.local:8000/v1"
    ).strip().strip('"').strip("'").rstrip("/")

    event_path = os.environ.get("GITHUB_EVENT_PATH", "")
    if not event_path or not os.path.exists(event_path):
        print("Error: GITHUB_EVENT_PATH not found.", file=sys.stderr)
        sys.exit(1)

    with open(event_path, "r", encoding="utf-8") as f:
        event_data = json.load(f)

    pr_data = event_data.get("pull_request")
    if not pr_data:
        print("Error: Event payload does not contain pull_request data.", file=sys.stderr)
        sys.exit(1)

    repo_full = event_data["repository"]["full_name"]
    pull_number = pr_data["number"]
    commit_id = pr_data["head"]["sha"]

    prompt_path = Path(__file__).parent / "system_prompt.md"
    if not prompt_path.exists():
        print(f"Error: {prompt_path} not found.", file=sys.stderr)
        sys.exit(1)
    system_prompt = prompt_path.read_text(encoding="utf-8")

    primary_token = pat_token if pat_token else actions_token
    primary_name = "UNITY_FRAMEWORK_PAT" if pat_token else "ACTIONS_GITHUB_TOKEN"
    primary_headers = {
        "Authorization": f"Bearer {primary_token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "SchellGames-AI-Reviewer"
    }

    fallback_name = None
    fallback_headers = None
    if actions_token and actions_token != primary_token:
        fallback_name = "ACTIONS_GITHUB_TOKEN"
        fallback_headers = {
            "Authorization": f"Bearer {actions_token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "SchellGames-AI-Reviewer"
        }

    qw_headers = {
        "Authorization": f"Bearer {qwen_api_key}",
        "Content-Type": "application/json"
    }

    files_url = f"{GH_BASE}/repos/{repo_full}/pulls/{pull_number}/files"
    files = http_request(files_url, headers=primary_headers)

    all_findings = []
    all_what_changed = []
    all_summaries = []
    reviewed_files = []
    unreviewed_files = []
    critical_count = 0
    high_count = 0

    for f in files:
        path = f["filename"]
        ext = os.path.splitext(path)[1].lower()
        patch = f.get("patch", "")

        if f.get("status") == "removed" or ext not in ALLOWED_EXTENSIONS or not patch:
            unreviewed_files.append(f"{path} (skipped: non-code file or removed)")
            continue

        reviewed_files.append(path)
        print(f"Analyzing: {path}")

        raw_content = ""
        try:
            content_resp = http_request(f["contents_url"], headers=primary_headers)
            raw_content = base64.b64decode(content_resp.get("content", "")).decode("utf-8", errors="replace")
        except Exception as e:
            print(f"Notice: Full content unavailable for {path} ({e}). Using patch only.")

        diff_lines, indent_map, line_content_map, annotated_diff = parse_diff_hunk(patch)

        if raw_content:
            numbered_lines = [f"Line {idx + 1}: {line}" for idx, line in enumerate(raw_content.splitlines())]
            numbered_file_context = "\n".join(numbered_lines)
        else:
            numbered_file_context = "Full file unavailable. Rely on unified diff."

        user_prompt = (
            f"File: {path}\n\n"
            f"=== ANNOTATED UNIFIED DIFF (Shows [Line N] for new code) ===\n{annotated_diff}\n\n"
            f"=== FULL FILE CONTENT (Line-Numbered) ===\n{numbered_file_context}"
        )

        qwen_body = {
            "model": "coder",
            "temperature": 0.2,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ]
        }

        try:
            resp = http_request(f"{qwen_api_base}/chat/completions", method="POST", headers=qw_headers, data=qwen_body)
            content = resp["choices"][0]["message"]["content"].strip()
            if content.startswith("```"):
                content = re.sub(r"^```(?:json)?\s*", "", content)
                content = re.sub(r"\s*```$", "", content)
            review_data = json.loads(content)
        except Exception as e:
            print(f"Warning: Inference or parsing failed for {path}: {e}")
            continue

        if review_data.get("file_summary"):
            all_summaries.append(review_data["file_summary"])
        if review_data.get("what_changed"):
            all_what_changed.append(review_data["what_changed"])

        diff_issues = review_data.get("diff_issues", [])
        for iss in diff_issues:
            line_num = int(iss["line_number"])
            sev = iss.get("severity", "High")
            if sev == "Critical":
                critical_count += 1
            elif sev == "High":
                high_count += 1

            suggested_fix = iss.get("suggested_fix", "")
            match = re.search(r"([a-zA-Z0-9_]+)\s*\(", suggested_fix)
            if match and line_num in line_content_map:
                sig = match.group(1)
                if sig not in line_content_map[line_num]:
                    if (line_num - 1) in line_content_map and sig in line_content_map[line_num - 1]:
                        line_num = line_num - 1
                    elif (line_num + 1) in line_content_map and sig in line_content_map[line_num + 1]:
                        line_num = line_num + 1

            orig_indent = indent_map.get(line_num, "")
            if orig_indent and not suggested_fix.startswith(orig_indent):
                suggested_fix = orig_indent + suggested_fix.lstrip()

            clean_title = iss.get("title", "").strip("*[] ")
            all_findings.append({
                "file": path,
                "line": line_num,
                "severity": sev,
                "title": clean_title
            })

            if line_num not in diff_lines:
                continue

            alert_tag = {
                "Critical": "[!CAUTION]",
                "High": "[!WARNING]",
                "Medium": "[!IMPORTANT]"
            }.get(sev, "[!NOTE]")

            desc_lines = iss.get("description", "").splitlines()
            quoted_desc = "\n".join(f"> {dl}" if dl.strip() else ">" for dl in desc_lines)
            comment_body = (
                f"> {alert_tag}\n"
                f"> **{clean_title}**\n"
                f">\n"
                f"{quoted_desc}\n\n"
                f"```suggestion\n"
                f"{suggested_fix}\n"
                f"```"
            )

            payload = {
                "commit_id": commit_id,
                "path": path,
                "line": line_num,
                "side": "RIGHT",
                "body": comment_body
            }

            comment_url = f"{GH_BASE}/repos/{repo_full}/pulls/{pull_number}/comments"
            if post_with_fallback(comment_url, payload, primary_name, primary_headers, fallback_name, fallback_headers):
                print(f"Posted inline comment on {path}:{line_num}")

    # Build Summary Comment
    if critical_count > 0:
        verdict = "### :red_circle: Changes requested"
    elif high_count > 0:
        verdict = "### :yellow_circle: Changes recommended"
    elif all_findings:
        verdict = "### :yellow_circle: Approved with recommendations"
    else:
        verdict = "### :green_circle: Approved"

    exec_summary = " ".join(all_summaries) if all_summaries else "All reviewed changes have been verified against project standards."

    findings_list = []
    sev_badges = {
        "Critical": ":red_circle: `Critical`",
        "High": ":large_orange_diamond: `High`",
        "Medium": ":yellow_circle: `Medium`",
        "Low": ":large_blue_circle: `Low`"
    }
    for item in all_findings:
        badge = sev_badges.get(item["severity"], ":large_blue_circle: `Low`")
        findings_list.append(f"- {badge} **{item['title']}** · `{item['file']}:{item['line']}`")

    findings_block = (
            f"<details open>\n<summary><b>{len(findings_list)} open findings</b></summary>\n<br/>\n\n"
            + "\n".join(findings_list)
            + "\n\n</details>"
    ) if findings_list else "<details>\n<summary><b>0 open findings</b></summary>\n<br/>\n\n- No blocking or major issues detected.\n\n</details>"

    what_changed_block = (
            f"<details>\n<summary><b>What changed in this PR</b></summary>\n<br/>\n\n"
            + "\n".join(f"- {wc}" for wc in all_what_changed)
            + "\n\n</details>"
    ) if all_what_changed else "<details>\n<summary><b>What changed in this PR</b></summary>\n<br/>\n\n- Updates across modified repository files.\n\n</details>"

    reviewed_block = (
            f"<details>\n<summary><b>Files reviewed ({len(reviewed_files)})</b></summary>\n<br/>\n\n"
            + "\n".join(f"- `{rf}`" for rf in reviewed_files)
            + "\n\n</details>"
    )

    unreviewed_block = (
            f"\n\n<details>\n<summary><b>Files not reviewed ({len(unreviewed_files)})</b></summary>\n<br/>\n\n"
            + "\n".join(f"- {uf}" for uf in unreviewed_files)
            + "\n\n</details>"
    ) if unreviewed_files else ""

    summary_markdown = f"{verdict}\n\n{exec_summary}\n\n{findings_block}\n\n{what_changed_block}\n\n{reviewed_block}{unreviewed_block}"

    issue_url = f"{GH_BASE}/repos/{repo_full}/issues/{pull_number}/comments"
    if post_with_fallback(issue_url, {"body": summary_markdown}, primary_name, primary_headers, fallback_name, fallback_headers):
        print("Posted review summary comment to PR discussion.")

    print("Review completed.")


if __name__ == "__main__":
    main()
