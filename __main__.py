"""
split_manifest.py - Generate, segment and retrieve Salesforce metadata.

Workflow:
  1. Generates a full package.xml from an org via sf CLI (--from-org required)
  2. Splits it into segmented manifests (auto-chunks types with >1000 members, can customize batch size for segment)
  3. Optionally retrieves them (-r), with search-priority filter (-s)

Usage:
    python split_manifest.py --from-org myAlias                          # Generate + split only
    python split_manifest.py --from-org myAlias --retrieve               # Generate + split + retrieve ALL
    python split_manifest.py --from-org myAlias --retrieve -s            # Generate + split + retrieve SEARCH-PRIORITY only
    python split_manifest.py -o myAlias -r -s
    python split_manifest.py --from-org myAlias --retrieve -s -p 4       # Idem, 4 retrieves in parallel
    python split_manifest.py --from-org myAlias --retrieve -s --dry-run  # Show what would be retrieved

No external packages required (stdlib only).
"""

import xml.etree.ElementTree as ET
import argparse
import json as _json
import os
import sys
import subprocess
import time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------

# Max members per manifest before auto-chunking a type into numbered files
MAX_MEMBERS_PER_MANIFEST = 1000

# Batch size override par segment (None = utiliser MAX_MEMBERS_PER_MANIFEST)
SEGMENT_BATCH_SIZES = {
    "objects_def": 50,
}

# search_priority = True  -> retrieved with -s (useful for grep / impact analysis)
# search_priority = False -> skipped with -s, retrieved only with --retrieve alone
SEGMENTS = {
    # -- SEARCH PRIORITY ---------------------------------------------------
    "apex": {
        "search_priority": True,
        "types": [
            "ApexClass", "ApexTrigger", "ApexComponent", "ApexPage",
            "ApexEmailNotifications",
        ],
    },
    "lwc": {
        "search_priority": True,
        "types": [
            "AuraDefinitionBundle", "LightningComponentBundle",
            "LightningMessageChannel",
        ],
    },
    "flows": {
        "search_priority": True,
        "types": ["Flow", "FlowDefinition"],
    },
    "automation": {
        "search_priority": True,
        "types": [
            "AnimationRule",
            "Workflow", "WorkflowAlert", "WorkflowFieldUpdate", "WorkflowRule",
            "WorkflowFlowAutomation", "ApprovalProcess",
        ],
    },
    "validation": {
        "search_priority": True,
        "types": ["ValidationRule"],
    },
    "email": {
        "search_priority": True,
        "types": ["EmailTemplate", "Letterhead"],
    },
    "objects_fields": {
        "search_priority": True,
        "types": ["CustomField"],
    },
    "objects_def": {
        "search_priority": True,
        "types": ["CustomObject"],
    },
    "objects_meta": {
        "search_priority": True,
        "types": [
            "RecordType", "BusinessProcess", "CompactLayout", "CustomIndex",
            "FieldRestrictionRule", "FieldSet", "WebLink",
        ],
    },
    "quickaction": {
        "search_priority": True,
        "types": ["QuickAction"],
    },
    "values": {
        "search_priority": True,
        "types": [
            "GlobalValueSet", "StandardValueSet",
            "CustomLabels", "CustomLabel",
        ],
    },
    "custommetadata": {
        "search_priority": True,
        "types": ["CustomMetadata"],
    },
    "pages": {
        "search_priority": True,
        "types": ["FlexiPage", "Layout", "UiViewDefinition"],
    },
    # -- NON PRIORITY ------------------------------------------------------
    "objects_views": {
        "search_priority": False,
        "types": ["ListView"],
    },
    "reports": {
        "search_priority": False,
        "types": ["Report", "ReportType", "Dashboard"],
    },
    "profiles": {
        "search_priority": False,
        "types": [
            "Profile", "ProfilePasswordPolicy", "ProfileSessionSetting",
            "Queue",
        ],
    },
    "translations": {
        "search_priority": False,
        "types": [
            "CustomObjectTranslation", "GlobalValueSetTranslation",
            "StandardValueSetTranslation", "Translations",
        ],
    },
    "settings": {
        "search_priority": False,
        "types": ["Settings"],
    },
    "security": {
        "search_priority": False,
        "types": [
            "PermissionSet", "PermissionSetGroup", "Role", "Group",
            "CustomPermission", "UserCriteria", "DuplicateRule",
            "MatchingRule", "MatchingRules",
            "SharingCriteriaRule", "SharingOwnerRule", "SharingRules",
            "SharingSet",
        ],
    },
    "experience": {
        "search_priority": False,
        "types": [
            "ExperienceBundle", "Network", "NetworkBranding", "ManagedTopics",
            "ModerationRule", "SiteDotCom", "CustomSite", "Community",
            "CommunityThemeDefinition", "BrandingSet", "Audience", "KeywordList",
        ],
    },
    "ui": {
        "search_priority": False,
        "types": [
            "CustomApplication", "CustomTab", "HomePageLayout",
            "NavigationMenu", "PathAssistant", "TopicsForObjects",
            "UiFormatSpecificationSet",
        ],
    },
    "connectivity": {
        "search_priority": False,
        "types": [
            "ConnectedApp", "AuthProvider", "Certificate", "NamedCredential",
            "RemoteSiteSetting", "CspTrustedSite", "SamlSsoConfig",
            "ExternalClientApplication", "ExtlClntAppConfigurablePolicies",
            "ExtlClntAppGlobalOauthSettings",
            "ExtlClntAppOauthConfigurablePolicies",
            "ExtlClntAppOauthSettings", "IframeWhiteListUrlSettings",
            "CleanDataService",
        ],
    },
    "misc": {
        "search_priority": False,
        "types": [
            "InstalledPackage", "AppMenu", "AppointmentSchedulingPolicy",
            "AssignmentRule", "AssignmentRules", "AutoResponseRules",
            "ContentAsset", "CustomNotificationType", "Document",
            "EscalationRules", "LeadConvertSettings", "ManagedContentType",
            "NotificationTypeConfig", "StaticResource",
        ],
    },
}

NS = "http://soap.sforce.com/2006/04/metadata"
ET.register_namespace("", NS)


# ---------------------------------------------------------------------------
# STEP 1: GENERATE PACKAGE.XML FROM ORG
# ---------------------------------------------------------------------------

def generate_manifest(org_alias: str, output_dir: str) -> Path:
    """Run sf project generate manifest --from-org to create the full package.xml."""
    manifest_dir = Path(output_dir)

    # Clean previous manifests (keep package.xml regeneration clean)
    if manifest_dir.exists():
        for f in manifest_dir.glob("*.xml"):
            f.unlink()
    manifest_dir.mkdir(parents=True, exist_ok=True)

    full_package = manifest_dir / "package.xml"

    cmd = [
        "sf", "project", "generate", "manifest",
        "--from-org", org_alias,
        "--name", "package",
        "--output-dir", str(manifest_dir),
    ]

    print(f"📡 Generating manifest from org '{org_alias}'...")
    print(f"   Command: {' '.join(cmd)}\n")

    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=300,
            shell=True,
            encoding="utf-8", errors="replace",
        )
        if result.returncode != 0:
            print(f"❌ sf CLI error:\n{result.stderr}", file=sys.stderr)
            sys.exit(1)
        if not full_package.exists():
            print(f"❌ Expected {full_package} not created by sf CLI.", file=sys.stderr)
            sys.exit(1)
    except FileNotFoundError:
        print("❌ 'sf' CLI not found in PATH. Install: npm install -g @salesforce/cli", file=sys.stderr)
        sys.exit(1)
    except subprocess.TimeoutExpired:
        print("❌ Manifest generation timed out after 5min.", file=sys.stderr)
        sys.exit(1)

    return full_package


# ---------------------------------------------------------------------------
# STEP 2: PARSE + SEGMENT
# ---------------------------------------------------------------------------

def parse_package(path: str) -> tuple[dict[str, list[str]], str]:
    """Parse package.xml -> {TypeName: [member, ...], ...}, api_version."""
    tree = ET.parse(path)
    root = tree.getroot()

    types_map = {}
    for t in root.findall(f"{{{NS}}}types"):
        name_el = t.find(f"{{{NS}}}name")
        if name_el is None:
            continue
        type_name = name_el.text
        members = [m.text for m in t.findall(f"{{{NS}}}members") if m.text]
        types_map[type_name] = members

    version_el = root.find(f"{{{NS}}}version")
    version = version_el.text if version_el is not None else "64.0"
    return types_map, version


def build_manifest_xml(types_dict: dict[str, list[str]], version: str) -> str:
    """Build a package.xml string from {TypeName: [members]}."""
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<Package xmlns="http://soap.sforce.com/2006/04/metadata">',
    ]
    for type_name in sorted(types_dict.keys()):
        members = sorted(types_dict[type_name])
        lines.append("    <types>")
        for m in members:
            safe = m.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            lines.append(f"        <members>{safe}</members>")
        lines.append(f"        <name>{type_name}</name>")
        lines.append("    </types>")
    lines.append(f"    <version>{version}</version>")
    lines.append("</Package>")
    return "\n".join(lines)


def chunk_list(lst: list, size: int) -> list[list]:
    """Split a list into chunks of at most `size` elements."""
    return [lst[i:i + size] for i in range(0, len(lst), size)]


def segment(package_path: str, output_dir: str) -> dict[str, Path]:
    """Split package.xml into segment manifests with auto-chunking.

    If a manifest ends up with > MAX_MEMBERS_PER_MANIFEST members
    AND contains a single type responsible for the overflow,
    that type is split across numbered files (e.g. objects_fields_1.xml, _2.xml).
    """
    types_map, version = parse_package(package_path)
    os.makedirs(output_dir, exist_ok=True)

    # Reverse lookup: TypeName -> segment_name
    type_to_segment = {}
    for seg_name, seg_conf in SEGMENTS.items():
        for t in seg_conf["types"]:
            type_to_segment[t] = seg_name

    # Dispatch members to segments
    segment_contents: dict[str, dict[str, list[str]]] = {}
    unmatched: dict[str, list[str]] = {}

    for type_name, members in types_map.items():
        seg = type_to_segment.get(type_name)
        if seg:
            segment_contents.setdefault(seg, {})[type_name] = members
        else:
            unmatched[type_name] = members

    if unmatched:
        segment_contents.setdefault("misc", {}).update(unmatched)
        print(f"   ⚠️  {len(unmatched)} type(s) not in config, added to misc.xml:")
        for t, m in unmatched.items():
            print(f"      - {t} ({len(m)} members)")
        print()

    # Write manifest files, with auto-chunking for large segments
    output_files: dict[str, Path] = {}

    for seg_name, seg_types in sorted(segment_contents.items()):
        total_members = sum(len(m) for m in seg_types.values())
        batch_size = SEGMENT_BATCH_SIZES.get(seg_name, MAX_MEMBERS_PER_MANIFEST)

        if total_members <= batch_size:
            # Normal case: single file
            xml = build_manifest_xml(seg_types, version)
            filepath = Path(output_dir) / f"{seg_name}.xml"
            filepath.write_text(xml, encoding="utf-8")
            output_files[seg_name] = filepath
        else:
            # Auto-chunk: find the dominant type(s) and split them
            # Strategy: split each type that exceeds the limit individually
            small_types = {}
            large_types = {}
            for tname, members in seg_types.items():
                if len(members) > batch_size:
                    large_types[tname] = members
                else:
                    small_types[tname] = members

            # Write small types as a single file (if any)
            if small_types:
                xml = build_manifest_xml(small_types, version)
                filepath = Path(output_dir) / f"{seg_name}.xml"
                filepath.write_text(xml, encoding="utf-8")
                output_files[seg_name] = filepath

            # Chunk each large type into numbered files
            for tname, members in large_types.items():
                chunks = chunk_list(sorted(members), batch_size)
                for i, chunk in enumerate(chunks, 1):
                    chunk_name = f"{seg_name}_{i}"
                    xml = build_manifest_xml({tname: chunk}, version)
                    filepath = Path(output_dir) / f"{chunk_name}.xml"
                    filepath.write_text(xml, encoding="utf-8")
                    output_files[chunk_name] = filepath

    return output_files


# ---------------------------------------------------------------------------
# STEP 3: RETRIEVE
# ---------------------------------------------------------------------------
def clean_sf_index():
    """Remove corrupted .sf source tracking index that causes isomorphic-git errors."""
    sf_dir = Path(".sf") / "orgs"
    if not sf_dir.exists():
        return 0
    count = 0
    for idx_file in sf_dir.rglob("localSourceTracking/index"):
        idx_file.unlink()
        count += 1
    return count

def retrieve_manifest(
    manifest_path: Path, org_alias: str, logs_dir: str
) -> tuple[str, bool, float, str]:
    """Run sf project retrieve start --json for a single manifest."""
    clean_sf_index()
    name = manifest_path.stem
    cmd = [
        "sf", "project", "retrieve", "start",
        "--manifest", str(manifest_path),
        "-o", org_alias,
        "-c",
        "--json",
    ]

    os.makedirs(logs_dir, exist_ok=True)
    log_path = os.path.join(logs_dir, f"{name}.log")

    start = time.time()
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=1800,
            shell=True,
            encoding="utf-8", errors="replace",
        )
        elapsed = time.time() - start
        success = result.returncode == 0

        # Parse JSON output for clean log entry
        summary = ""
        try:
            data = _json.loads(result.stdout)
            status_val = data.get("status", result.returncode)
            files = data.get("result", {}).get("files", [])
            msg = data.get("message", "")
            summary = f"status={status_val}, files={len(files)}"
            if msg:
                summary += f", message={msg}"
        except (_json.JSONDecodeError, AttributeError):
            # Fallback: sf may print non-JSON warnings before the JSON
            summary = f"returncode={result.returncode}"
            if result.stderr:
                # Extract meaningful lines from stderr (skip ANSI/spinner junk)
                err_lines = [
                    l.strip() for l in result.stderr.splitlines()
                    if l.strip() and not l.strip().startswith("[2K")
                ]
                if err_lines:
                    summary += f"\n  {err_lines[-1]}"

        # Append to log file (stack executions)
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        icon = "OK" if success else "FAIL"
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(f"[{timestamp}] {icon} ({elapsed:.0f}s) {summary}\n")
            if not success:
                # Log full stderr for debugging failed retrieves
                f.write(f"--- stderr ---\n{result.stderr}\n--- end ---\n")

        return name, success, elapsed, summary[:200]
    except subprocess.TimeoutExpired:
        elapsed = time.time() - start
        msg = f"TIMEOUT after {elapsed:.0f}s"
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(f"[{timestamp}] TIMEOUT ({elapsed:.0f}s)\n")
        return name, False, elapsed, msg
    except FileNotFoundError:
        return name, False, 0.0, "ERROR: 'sf' CLI not found in PATH"


def run_retrieves(
    manifest_files: dict[str, Path],
    search_only: bool,
    org_alias: str,
    parallel: int,
    logs_dir: str,
    dry_run: bool,
):
    """Run retrieves, optionally filtered to search-priority only."""
    to_retrieve = {}
    skipped = []

    for seg_name, filepath in manifest_files.items():
        # For chunked files like objects_fields_1, inherit priority from base segment
        base_name = seg_name.rsplit("_", 1)[0] if seg_name[-1].isdigit() and "_" in seg_name else seg_name
        is_priority = SEGMENTS.get(base_name, {}).get("search_priority", False)
        if search_only and not is_priority:
            skipped.append(seg_name)
        else:
            to_retrieve[seg_name] = filepath

    if skipped:
        print(f"⏭️  Skipped (non-priority): {', '.join(sorted(skipped))}")
        print(f"   Retrieve them manually:")
        for s in sorted(skipped):
            print(f"     sf project retrieve start --manifest manifest/{s}.xml -o {org_alias} -c")
        print()

    if not to_retrieve:
        print("Nothing to retrieve.")
        return

    total_members = 0
    print(f"🚀 {'[DRY RUN] Would retrieve' if dry_run else 'Retrieving'} "
          f"{len(to_retrieve)} manifests (parallel={parallel}):\n")
    for name in sorted(to_retrieve):
        seg_types, _ = parse_package(str(to_retrieve[name]))
        member_count = sum(len(m) for m in seg_types.values())
        total_members += member_count
        base_name = name.rsplit("_", 1)[0] if name[-1].isdigit() and "_" in name else name
        is_priority = SEGMENTS.get(base_name, {}).get("search_priority", False)
        tag = "🔍" if is_priority else "📦"
        print(f"   {tag} {name:<25} {len(seg_types):>3} types  {member_count:>5} members")
    print(f"\n   Total: {total_members} members")

    if dry_run:
        print("\n   (dry run, no retrieve executed)")
        return

    print()
    total_start = time.time()
    results = []

    with ThreadPoolExecutor(max_workers=parallel) as executor:
        futures = {
            executor.submit(retrieve_manifest, filepath, org_alias, logs_dir): seg_name
            for seg_name, filepath in to_retrieve.items()
        }
        for future in as_completed(futures):
            name, success, elapsed, excerpt = future.result()
            icon = "✅" if success else "❌"
            minutes = elapsed / 60
            print(f"   {icon} {name:<25} {minutes:>5.1f}min")
            if not success:
                print(f"      └─ {excerpt}")
            results.append((name, success, elapsed))

    total_elapsed = time.time() - total_start
    ok = sum(1 for _, s, _ in results if s)
    ko = len(results) - ok
    print(f"\n{'─' * 50}")
    print(f"Done in {total_elapsed / 60:.1f}min — {ok} succeeded, {ko} failed")
    print(f"Logs: {logs_dir}/")


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Generate, segment, and retrieve Salesforce metadata.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python split_manifest.py --from-org myAlias
  python split_manifest.py --from-org myAlias --retrieve -s
  python split_manifest.py --from-org myAlias --retrieve --parallel 4
  python split_manifest.py --from-org myAlias --retrieve -s --dry-run
        """,
    )
    parser.add_argument(
        "--from-org", "-o", required=True,
        help="Salesforce org alias (passed to sf CLI for manifest generation and retrieves)",
    )
    parser.add_argument(
        "--output-dir", "-d", default="manifest",
        help="Output directory for manifests (default: manifest/)",
    )
    parser.add_argument(
        "--retrieve", "-r", action="store_true",
        help="Run sf retrieve after splitting",
    )
    parser.add_argument(
        "--search", "-s", action="store_true",
        help="Retrieve ONLY search-priority manifests",
    )
    parser.add_argument(
        "--parallel", "-p", type=int, default=3,
        help="Max parallel retrieves (default: 3)",
    )
    parser.add_argument(
        "--logs-dir", default="logs",
        help="Directory for retrieve logs (default: logs/)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Show what would be retrieved without executing",
    )

    args = parser.parse_args()

    # -- Step 1: Generate manifest from org --------------------------------
    full_package = generate_manifest(args.from_org, args.output_dir)

    # -- Step 2: Parse and show stats --------------------------------------
    types_map, version = parse_package(str(full_package))
    total_types = len(types_map)
    total_members = sum(len(m) for m in types_map.values())
    print(f"📂 Generated {full_package}")
    print(f"   {total_types} types, {total_members} members (API v{version})\n")

    # -- Step 3: Segment ---------------------------------------------------
    print(f"✂️  Splitting into segmented manifests...\n")
    manifest_files = segment(str(full_package), args.output_dir)

    for seg_name, filepath in sorted(manifest_files.items()):
        base_name = seg_name.rsplit("_", 1)[0] if seg_name[-1].isdigit() and "_" in seg_name else seg_name
        is_priority = SEGMENTS.get(base_name, {}).get("search_priority", False)
        tag = "🔍" if is_priority else "📦"
        seg_types, _ = parse_package(str(filepath))
        member_count = sum(len(m) for m in seg_types.values())
        print(f"   {tag} {filepath.name:<30} {len(seg_types):>3} types  {member_count:>5} members")

    print(f"\n   🔍 = search-priority (retrieved with -s)")
    print(f"   📦 = non-priority   (skipped with -s)")
    print(f"   Total: {len(manifest_files)} manifests\n")

    # -- Step 4: Retrieve --------------------------------------------------
    if args.retrieve or args.dry_run:
        run_retrieves(
            manifest_files, args.search, args.from_org,
            args.parallel, args.logs_dir, args.dry_run,
        )
    else:
        print("💡 To retrieve, re-run with --retrieve (add -s for search-priority only)")
        print(f"💡 To preview: add --dry-run")


if __name__ == "__main__":
    main()