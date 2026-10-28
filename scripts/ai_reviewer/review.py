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
    ".yml", ".yaml", ".Jenkinsfile", ".asmdef", ".asmref"
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
    diff_lines_list = []

    if not patch:
        return valid_lines, indent_map, line_content_map, diff_lines_list

    cur = 0
    for line in patch.splitlines():
        match = re.match(r"^@@ -\d+(?:,\d+)? \+(\d+)", line)
        if match:
            cur = int(match.group(1))
        elif line.startswith("+") and not line.startswith("+++"):
            valid_lines.add(cur)
            diff_lines_list.append(cur)
            content = line[1:]
            line_content_map[cur] = content
            indent_match = re.match(r"^(\s+)", content)
            indent_map[cur] = indent_match.group(1) if indent_match else ""
            cur += 1
        elif line.startswith(" "):
            valid_lines.add(cur)
            diff_lines_list.append(cur)
            content = line[1:]
            line_content_map[cur] = content
            indent_match = re.match(r"^(\s+)", content)
            indent_map[cur] = indent_match.group(1) if indent_match else ""
            cur += 1

    return valid_lines, indent_map, line_content_map, diff_lines_list


def format_multiline_suggestion(suggested_fix, base_indent):
    if not suggested_fix:
        return ""
    lines = suggested_fix.rstrip().splitlines()
    if not lines:
        return ""

    first_non_empty = next((l for l in lines if l.strip()), None)
    if not first_non_empty:
        return "\n".join(lines)

    first_indent_match = re.match(r"^(\s*)", first_non_empty)
    first_indent_len = len(first_indent_match.group(1)) if first_indent_match else 0

    adjusted = []
    for l in lines:
        if not l.strip():
            adjusted.append("")
            continue
        cur_indent_match = re.match(r"^(\s*)", l)
        cur_indent_len = len(cur_indent_match.group(1)) if cur_indent_match else 0
        rel_indent = max(0, cur_indent_len - first_indent_len)
        adjusted.append(f"{base_indent}{' ' * rel_indent}{l.lstrip()}")

    return "\n".join(adjusted)


def is_redundant_guard_suggestion(line_num, suggested_fix, full_file_lines):
    if not suggested_fix or not full_file_lines:
        return False

    idx = line_num - 1
    next_idx = line_num
    if 0 <= next_idx < len(full_file_lines):
        next_line = full_file_lines[next_idx].strip()
        curr_line = full_file_lines[idx].strip() if 0 <= idx < len(full_file_lines) else ""
        s_fix = suggested_fix.strip()

        has_exit = next_line.startswith("return") or next_line.startswith("throw")
        suggests_exit = "return" in s_fix or "throw" in s_fix
        curr_had_no_exit = "return" not in curr_line and "throw" not in curr_line

        if has_exit and suggests_exit and curr_had_no_exit:
            return True

    return False


def find_enclosing_asmdef(target_path):
    """Walks up directory hierarchy from target file to find its controlling .asmdef."""
    cur = Path(target_path).parent
    while cur != cur.parent:
        asmdefs = list(cur.glob("*.asmdef"))
        if asmdefs:
            try:
                content = asmdefs[0].read_text(encoding="utf-8", errors="replace")
                return str(asmdefs[0]), content
            except Exception:
                pass
        cur = cur.parent
    return None, ""


def find_companion_production_files(target_path, diff_content):
    found_files = {}
    target_name = Path(target_path).name.lower()

    stem_candidate = re.sub(r"(?i)(?:_?tests?|_?fixtures?)\.cs$", "", target_name)
    target_file_candidate = f"{stem_candidate}.cs" if stem_candidate != target_name else None

    symbols = set(re.findall(r"\b([A-Z][a-zA-Z0-9_]{2,})\b", diff_content))
    ignored_keywords = {
        "Assert", "Test", "SetUp", "TearDown", "IsTrue", "IsFalse",
        "AreEqual", "That", "UnityTest", "IEnumerator", "GameObject",
        "String", "Int32", "Single", "Boolean", "Object", "List", "Dictionary"
    }
    symbols -= ignored_keywords

    try:
        for p in Path(".").rglob("*.cs"):
            p_parts_lower = [part.lower() for part in p.parts]
            if any(t in part for part in p_parts_lower for t in ["test", "fixture", "editor/tests"]):
                continue

            file_stem = p.stem.lower()
            file_name = p.name.lower()

            if target_file_candidate and file_name == target_file_candidate:
                if str(p) not in found_files:
                    found_files[str(p)] = p.read_text(encoding="utf-8", errors="replace")

            for sym in list(symbols):
                if file_stem == sym.lower():
                    if str(p) not in found_files:
                        found_files[str(p)] = p.read_text(encoding="utf-8", errors="replace")
                    symbols.remove(sym)

            if len(found_files) >= 3:
                break
    except Exception as e:
        print(f"  Notice: Error during companion file scan: {e}")

    return found_files


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
    pr_title = pr_data.get("title", "")
    pr_body = pr_data.get("body", "") or ""

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

    commits_url = f"{GH_BASE}/repos/{repo_full}/pulls/{pull_number}/commits"
    commit_messages = []
    try:
        commits_data = http_request(commits_url, headers=primary_headers)
        for c in commits_data:
            msg = c.get("commit", {}).get("message", "").strip()
            if msg:
                commit_messages.append(f"- {msg.splitlines()[0]}")
    except Exception as e:
        print(f"Notice: Could not fetch commit history: {e}")
    commits_summary_text = "\n".join(commit_messages) if commit_messages else "(No commit messages provided)"

    files_url = f"{GH_BASE}/repos/{repo_full}/pulls/{pull_number}/files"
    files = http_request(files_url, headers=primary_headers)

    all_diffs_map = {}
    for file_info in files:
        f_name = file_info.get("filename", "")
        patch = file_info.get("patch", "")
        if patch:
            all_diffs_map[f_name] = patch

    all_findings = []
    all_what_changed = []
    all_summaries = []
    all_confidence_notes = []
    all_deliberations = []
    reviewed_files = []
    unreviewed_files = []
    batched_comments = []
    file_level_findings = []
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
        local_path = Path(path)
        if local_path.is_file():
            try:
                raw_content = local_path.read_text(encoding="utf-8", errors="replace")
                print(f"  Loaded {path} directly from local disk ({len(raw_content.splitlines())} lines).")
            except Exception as e:
                print(f"  Warning: Local read failed for {path}: {e}")

        if not raw_content and "contents_url" in f:
            try:
                content_resp = http_request(f["contents_url"], headers=primary_headers)
                raw_content = base64.b64decode(content_resp.get("content", "")).decode("utf-8", errors="replace")
            except Exception as e:
                print(f"Notice: Full content unavailable for {path} ({e}). Using patch only.")

        diff_lines, indent_map, line_content_map, diff_lines_sorted = parse_diff_hunk(patch)
        full_file_lines = raw_content.splitlines() if raw_content else []

        if full_file_lines:
            numbered_lines = [f"Line {idx + 1}: {line}" for idx, line in enumerate(full_file_lines)]
            numbered_file_context = "\n".join(numbered_lines)
        else:
            numbered_file_context = "Full file unavailable. Rely on unified diff."

        other_diff_blocks = []
        for other_name, other_patch in all_diffs_map.items():
            if other_name != path:
                patch_lines = other_patch.splitlines()
                if len(patch_lines) > 60:
                    truncated_patch = "\n".join(patch_lines[:60]) + "\n... [diff truncated for length]"
                else:
                    truncated_patch = other_patch
                other_diff_blocks.append(f"--- {other_name} ---\n{truncated_patch}")
        global_diffs_text = "\n\n".join(other_diff_blocks) if other_diff_blocks else "(No other files modified in this PR)"

        # Search for controlling .asmdef in directory hierarchy
        asmdef_path, asmdef_content = find_enclosing_asmdef(path)
        asmdef_block = ""
        if asmdef_content:
            asmdef_block = (
                f"=== ENCLOSING ASSEMBLY DEFINITION ({asmdef_path}) ===\n"
                f"Note: This asmdef governs assembly references and versionDefines for this file.\n"
                f"{asmdef_content}\n\n"
            )
            print(f"  [Context Injection] Found controlling asmdef: {asmdef_path}")

        # Search for companion production classes
        companion_files = find_companion_production_files(path, patch)
        companion_blocks = []
        for comp_path, comp_content in companion_files.items():
            comp_lines = comp_content.splitlines()
            if len(comp_lines) > 300:
                truncated_comp = "\n".join(comp_lines[:300]) + "\n... [file truncated for length]"
            else:
                truncated_comp = comp_content
            companion_blocks.append(
                f"--- COMPANION PRODUCTION FILE: {comp_path} ---\n"
                f"Note: This is the production implementation. Cross-reference signatures, parameter names, and XML doc contracts.\n"
                f"{truncated_comp}"
            )
            print(f"  [Context Injection] Injected companion production file: {comp_path} ({len(comp_lines)} lines)")

        companion_code_text = "\n\n".join(companion_blocks) + "\n\n" if companion_blocks else ""

        user_prompt = (
            f"=== PULL REQUEST CONTEXT ===\n"
            f"Title: {pr_title}\n"
            f"Description:\n{pr_body if pr_body.strip() else '(No PR description provided)'}\n\n"
            f"Commit History:\n{commits_summary_text}\n\n"
            f"All Files Modified in this PR (Unified Diffs):\n{global_diffs_text}\n\n"
            f"{asmdef_block}"
            f"{companion_code_text}"
            f"=== TARGET FILE BEING REVIEWED: {path} ===\n\n"
            f"=== MODIFICATIONS IN THIS FILE (Unified Diff) ===\n{patch}\n\n"
            f"=== FULL FILE CONTENT (Line-Numbered) ===\n{numbered_file_context}\n\n"
            f"CRITICAL REVIEW DIRECTIVES:\n"
            f"1. Context Verification: Before evaluating a call in a test file, verify the method's actual signature, parameter names, and XML documentation from the companion production code.\n"
            f"2. High Confidence Only: Only report issues that represent genuine bugs or unhandled failures. Never speculate or question intentional boundary parameters.\n"
            f"3. Concrete Multi-Line Suggestions: Every reported issue MUST provide an executable code fix in 'suggested_fix'."
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

        conf_note = (review_data.get("confidence_notes") or "").strip()
        if conf_note:
            all_confidence_notes.append((path, conf_note))

        delib_note = (review_data.get("deliberation") or "").strip()
        if delib_note:
            all_deliberations.append((path, delib_note))

        diff_issues = review_data.get("diff_issues", [])
        for iss in diff_issues:
            line_num = int(iss["line_number"])
            start_line = iss.get("start_line")
            if start_line is not None:
                try:
                    start_line = int(start_line)
                except (ValueError, TypeError):
                    start_line = None

            sev = iss.get("severity", "High")
            suggested_fix = (iss.get("suggested_fix") or "").strip()

            current_line_text = line_content_map.get(line_num, "").strip()
            if suggested_fix and suggested_fix == current_line_text:
                continue

            if is_redundant_guard_suggestion(line_num, suggested_fix, full_file_lines):
                continue

            if sev == "Critical":
                critical_count += 1
            elif sev == "High":
                high_count += 1

            base_indent = ""
            if 0 <= line_num - 1 < len(full_file_lines):
                match = re.match(r"^(\s*)", full_file_lines[line_num - 1])
                base_indent = match.group(1) if match else ""
            formatted_fix = format_multiline_suggestion(suggested_fix, base_indent)

            clean_title = iss.get("title", "").strip("*[] ")
            location_str = f"`{path}:{line_num}`" if not start_line else f"`{path}:{start_line}-{line_num}`"
            all_findings.append({
                "file": path,
                "location": location_str,
                "severity": sev,
                "title": clean_title
            })

            alert_tag = {
                "Critical": "[!CAUTION]",
                "High": "[!WARNING]",
                "Medium": "[!IMPORTANT]"
            }.get(sev, "[!NOTE]")

            desc_lines = iss.get("description", "").splitlines()
            quoted_desc = "\n".join(f"> {dl}" if dl.strip() else ">" for dl in desc_lines)
            suggestion_block = f"\n\n```suggestion\n{formatted_fix}\n```" if formatted_fix else ""

            if line_num in diff_lines:
                comment_body = (
                    f"> {alert_tag}\n"
                    f"> **{clean_title}**\n"
                    f">\n"
                    f"{quoted_desc}"
                    f"{suggestion_block}"
                )
                comment_obj = {
                    "path": path,
                    "line": line_num,
                    "side": "RIGHT",
                    "body": comment_body
                }
                if start_line and start_line in diff_lines and start_line < line_num:
                    comment_obj["start_line"] = start_line
                    comment_obj["start_side"] = "RIGHT"
                batched_comments.append(comment_obj)
            else:
                nearest_diff_line = None
                if diff_lines_sorted:
                    nearest_diff_line = min(diff_lines_sorted, key=lambda dl: abs(dl - line_num))

                if nearest_diff_line and abs(nearest_diff_line - line_num) <= 15:
                    comment_body = (
                        f"> {alert_tag}\n"
                        f"> **{clean_title}** *(Applies to line {line_num})*\n"
                        f">\n"
                        f"{quoted_desc}"
                        f"{suggestion_block}"
                    )
                    batched_comments.append({
                        "path": path,
                        "line": nearest_diff_line,
                        "side": "RIGHT",
                        "body": comment_body
                    })
                else:
                    code_preview = f"\n```csharp\n{formatted_fix}\n```" if formatted_fix else ""
                    file_level_findings.append(
                        f"#### {alert_tag} `{path}:{line_num}` — {clean_title}\n\n"
                        f"{iss.get('description', '')}\n{code_preview}"
                    )

    # Build Summary Content
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
        findings_list.append(f"- {badge} **{item['title']}** · {item['location']}")

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

    confidence_block = ""
    if all_confidence_notes:
        notes_lines = [f"- **`{p}`**: {note}" for p, note in all_confidence_notes]
        confidence_block = (
                f"\n\n### :mag: Complex Areas & Copilot Recommendation\n\n"
                f"The following areas contain complexity or external dependencies that could not be verified with complete confidence. "
                f"A secondary review with **GitHub Copilot** (`@copilot review`) is recommended for these sections:\n\n"
                + "\n".join(notes_lines)
        )

    broader_section = ""
    if file_level_findings:
        broader_section = "\n\n### Broader File & Context Findings\n\n" + "\n\n---\n\n".join(file_level_findings)

    thinking_sub_blocks = []
    for p, thought in all_deliberations:
        safe_thought = thought[:1500] + ("\n... [truncated for length]" if len(thought) > 1500 else "")
        quoted_thought = "\n".join(f"> {l}" if l.strip() else ">" for l in safe_thought.splitlines())
        thinking_sub_blocks.append(
            f"<details>\n<summary><code>{p}</code></summary>\n\n{quoted_thought}\n\n</details>"
        )

    thinking_block = (
            f"\n\n<details>\n<summary>🧠 <b>Reviewer Deliberation & Thought Process</b> (Click to expand)</summary>\n<br/>\n\n"
            + "\n\n".join(thinking_sub_blocks)
            + "\n\n</details>"
    ) if thinking_sub_blocks else ""

    summary_markdown = f"{verdict}\n\n{exec_summary}\n\n{findings_block}\n\n{what_changed_block}\n\n{reviewed_block}{unreviewed_block}{confidence_block}{broader_section}{thinking_block}"

    dry_run = os.environ.get("DRY_RUN", "").lower() in ("true", "1", "yes")

    review_url = f"{GH_BASE}/repos/{repo_full}/pulls/{pull_number}/reviews"
    review_payload = {
        "commit_id": commit_id,
        "body": summary_markdown,
        "event": "COMMENT",
        "comments": batched_comments
    }

    if dry_run:
        print("\n" + "=" * 30 + " DRY RUN MODE ENABLED " + "=" * 30)
        print(f"Target PR: #{pull_number} ({repo_full})")
        print(f"Commit SHA: {commit_id}")
        print(f"Inline Comments Prepared: {len(batched_comments)}")
        print("\n--- BATCH REVIEW PAYLOAD ---")
        print(json.dumps(review_payload, indent=2))
        print("=" * 82)
        print("DRY RUN COMPLETE: No requests were sent to GitHub.")
        return

    print(f"Submitting batched review with {len(batched_comments)} inline comment(s)...")
    success = post_with_fallback(
        review_url,
        review_payload,
        primary_name,
        primary_headers,
        fallback_name,
        fallback_headers
    )

    if not success and batched_comments:
        print("Batched review rejected. Retrying review submission without inline comments to preserve summary...")
        fallback_payload = {
            "commit_id": commit_id,
            "body": summary_markdown + "\n\n*(Note: Inline diff comments could not be attached due to diff alignment)*",
            "event": "COMMENT",
            "comments": []
        }
        post_with_fallback(
            review_url,
            fallback_payload,
            primary_name,
            primary_headers,
            fallback_name,
            fallback_headers
        )

    print("Review completed.")


if __name__ == "__main__":
    main()