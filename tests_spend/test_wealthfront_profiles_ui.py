"""Headless-Chrome tests for P6: grouped Sources tab, Add profile, Configure, Escape, and XSS."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests_spend.test_wealthfront_ui import (
    FAST,
    browser,
    build_page,
    needs_chrome,
    probe,
)


def p6_settings_fixture() -> dict:
    """Realistic P5/P6 /api/connections fixture covering grouped profiles,
    eligible & duplicate detected candidates, not_found local, single-profile
    local (Traycer), and API source.
    """
    return {
        "ok": True,
        "connections": [
            {
                "source": "claude_local",
                "name": "Claude Code",
                "kind": "local",
                "state": "receiving",
                "supportsProfiles": True,
                "revision": 4,
                "profiles": [
                    {
                        "key": "default",
                        "name": "Personal",
                        "location": "C:/Users/me/.claude",
                        "origin": "default",
                        "state": "receiving",
                        "revision": 4,
                        "plan": "Claude Max",
                        "sharesLimitsWith": None,
                    },
                    {
                        "key": "claude-code-work",
                        "name": "Work",
                        "location": "C:/Users/me/.claude-work",
                        "origin": "manual",
                        "state": "added",
                        "revision": 2,
                        "plan": "Claude Pro",
                        "sharesLimitsWith": "default",
                    },
                ],
                "detected": [
                    {
                        "candidateId": "cand-fresh",
                        "folder": "~/.claude-fresh",
                        "plan": "Claude Pro",
                        "sameAccountAs": None,
                    },
                    {
                        "candidateId": "cand-dup",
                        "folder": "~/.claude-dup",
                        "plan": "Claude Max",
                        "sameAccountAs": "default",
                    },
                ],
            },
            {
                "source": "codex_local",
                "name": "Codex",
                "kind": "local",
                "state": "not_found",
                "supportsProfiles": True,
                "revision": 1,
                "profiles": [
                    {
                        "key": "default",
                        "name": "Default",
                        "location": "",
                        "origin": "default",
                        "state": "not_found",
                        "revision": 1,
                        "plan": None,
                        "sharesLimitsWith": None,
                    }
                ],
                "detected": [],
            },
            {
                "source": "traycer_local",
                "name": "Traycer",
                "kind": "local",
                "state": "receiving",
                "supportsProfiles": False,
                "revision": 1,
                "profiles": [
                    {
                        "key": "default",
                        "name": "Default",
                        "location": "C:/Users/me/.traycer",
                        "origin": "default",
                        "state": "receiving",
                        "revision": 1,
                        "plan": None,
                        "sharesLimitsWith": None,
                    }
                ],
                "detected": [],
            },
            {
                "source": "openrouter",
                "name": "OpenRouter balance",
                "kind": "api",
                "state": "connected",
                "supportsProfiles": False,
                "detail": "Prepaid credits connected",
                "revision": 3,
                "profiles": [
                    {
                        "key": "default",
                        "name": "Default",
                        "location": "",
                        "origin": "default",
                        "state": "connected",
                        "revision": 3,
                        "plan": None,
                        "sharesLimitsWith": None,
                    }
                ],
                "detected": [],
            },
        ],
        "machines": [{"id": "pc", "name": "Desk PC", "online": True}],
        "setup": {},
        "hubAvailable": False,
    }


def _p6_api_js() -> str:
    return """
    api.browse_location = async function (kind) {
      window.__calls.push(["browse_location", kind]);
      return "C:/Users/me/.claude-custom";
    };
    api.connect_source = async function (s, isApi, val, rev, profile) {
      window.__calls.push(["connect_source", s, isApi, val, rev, profile]);
      return { ok: true };
    };
    api.disconnect_source = async function (s, rev, profile) {
      window.__calls.push(["disconnect_source", s, rev, profile]);
      return { ok: true };
    };
    api.add_profile = async function (s, candId, loc, name) {
      window.__calls.push(["add_profile", s, candId, loc, name]);
      return { ok: true, connection: {} };
    };
    api.rename_profile = async function (s, prof, name, rev) {
      window.__calls.push(["rename_profile", s, prof, name, rev]);
      return { ok: true, connection: {} };
    };
    api.remove_profile = async function (s, prof, rev) {
      window.__calls.push(["remove_profile", s, prof, rev]);
      return { ok: true, disabled: prof === "default", removed: prof !== "default" };
    };
    api.dismiss_candidate = async function (s, candId) {
      window.__calls.push(["dismiss_candidate", s, candId]);
      return { ok: true, dismissed: true };
    };
    """


GROUPED_MARKUP_PROBE = """
await until(() => document.querySelector('.provider-row'));
await openSettings('sources');
await wait(80);
const body = document.getElementById('sheetBody');
const sub = body.querySelector('.card-subtitle').textContent.replace(/\\s+/g, ' ').trim();
const rows = [...body.querySelectorAll('.source-row')].map(r => ({
  sub: r.classList.contains('sub'),
  missing: r.classList.contains('is-missing'),
  name: (r.querySelector('.source-name') || {}).textContent || '',
  meta: (r.querySelector('.source-meta') || {}).textContent || '',
  badge: (r.querySelector('.state-badge') || {}).textContent || '',
  badgeClass: (r.querySelector('.state-badge') || {}).className || '',
  buttons: [...r.querySelectorAll('button')].map(b => ({
    id: b.id,
    text: b.textContent.replace(/\\s+/g, ' ').trim(),
    source: b.dataset.source || '',
    profile: b.dataset.profile || '',
    candidate: b.dataset.candidate || '',
  })),
}));
const addButtons = [...body.querySelectorAll('[id^="addProfBtn_"]')].map(b => ({
  id: b.id,
  source: b.dataset.source,
  text: b.textContent.replace(/\\s+/g, ' ').trim(),
}));
report({ sub, rows, addButtons });
"""


@needs_chrome
@browser
def test_grouped_sources_tab_markup_and_counts(tmp_path: Path):
    html = build_page(
        pref="light",
        probe=GROUPED_MARKUP_PROBE,
        settings=p6_settings_fixture(),
        init={"demo": False},
        extra_css=FAST,
    )
    r = probe(html, 380, tmp_path, height=900)
    # 4 providers (claude_local, codex_local, traycer_local, openrouter);
    # claude has 2 non-not_found profiles, codex has 0 (not_found), traycer 1, openrouter 1 => 4 profiles
    assert r["sub"] == "4 providers · 4 profiles"

    rows = r["rows"]
    # Group header for Claude Code
    claude_hdr = rows[0]
    assert claude_hdr["sub"] is False
    assert claude_hdr["name"] == "Claude Code"
    assert "2 profiles" in claude_hdr["meta"]
    assert "1 found, not added" in claude_hdr["meta"]

    # Default Personal profile
    personal_row = rows[1]
    assert personal_row["sub"] is True
    assert personal_row["name"] == "Personal"
    assert personal_row["badge"] == "receiving"
    assert "ok" in personal_row["badgeClass"]
    assert personal_row["buttons"][0]["id"] == "cfg_claude_local"
    assert personal_row["buttons"][0]["text"] == "Configure"

    # Secondary Work profile with sharesLimitsWith: "default" -> Personal
    work_row = rows[2]
    assert work_row["sub"] is True
    assert work_row["name"] == "Work"
    assert "Shares limits with Personal" in work_row["meta"]
    assert work_row["badge"] == "added"
    assert work_row["buttons"][0]["id"] == "cfg_claude_local__claude-code-work"
    assert work_row["buttons"][0]["text"] == "Configure"

    # Eligible detected folder: detected badge + Add + Dismiss
    fresh_row = rows[3]
    assert fresh_row["sub"] is True
    assert fresh_row["name"] == "~/.claude-fresh"
    assert fresh_row["badge"] == "detected"
    assert "new" in fresh_row["badgeClass"]
    assert [b["text"] for b in fresh_row["buttons"]] == ["Add", "Dismiss"]
    assert fresh_row["buttons"][0]["candidate"] == "cand-fresh"

    # Duplicate detected candidate (sameAccountAs: "default"): same account badge, NO Add button
    dup_row = rows[4]
    assert dup_row["sub"] is True
    assert dup_row["name"] == "~/.claude-dup"
    assert "Same account as Personal" in dup_row["meta"]
    assert dup_row["badge"] == "same account"
    assert [b["text"] for b in dup_row["buttons"]] == ["Dismiss"]

    # Local not_found provider: not found badge + Choose folder button
    codex_row = rows[5]
    assert codex_row["sub"] is False and codex_row["missing"] is True
    assert codex_row["name"] == "Codex"
    assert codex_row["badge"] == "not found"
    assert [b["text"] for b in codex_row["buttons"]] == ["Choose folder"]

    # Traycer single-profile (supportsProfiles: false)
    traycer_row = rows[6]
    assert traycer_row["name"] == "Traycer"
    assert [b["text"] for b in traycer_row["buttons"]] == ["Configure"]

    # API provider (supportsProfiles: false)
    openrouter_row = rows[7]
    assert openrouter_row["name"] == "OpenRouter balance"
    assert openrouter_row["badge"] == "connected"
    assert [b["text"] for b in openrouter_row["buttons"]] == ["Configure"]

    # Add profile buttons appear only on supportsProfiles: true providers
    assert r["addButtons"] == [
        {
            "id": "addProfBtn_claude_local",
            "source": "claude_local",
            "text": "+ Add another profile",
        },
        {
            "id": "addProfBtn_codex_local",
            "source": "codex_local",
            "text": "+ Add profile",
        },
    ]


@needs_chrome
@browser
def test_sources_header_counts_exclude_not_found_profiles(tmp_path: Path):
    settings = p6_settings_fixture()
    settings["connections"].append(
        {
            "source": "opencode_local",
            "name": "OpenCode Go",
            "kind": "local",
            "state": "not_found",
            "supportsProfiles": True,
            "revision": 1,
            "profiles": [
                {
                    "key": "default",
                    "name": "Default",
                    "location": "",
                    "origin": "default",
                    "state": "not_found",
                    "revision": 1,
                    "plan": None,
                    "sharesLimitsWith": None,
                }
            ],
            "detected": [],
        }
    )
    html = build_page(
        pref="light",
        probe=GROUPED_MARKUP_PROBE,
        settings=settings,
        init={"demo": False},
        extra_css=FAST,
    )
    r = probe(html, 380, tmp_path, height=900)
    assert r["sub"] == "5 providers · 4 profiles"


ADD_PROFILE_FLOW_PROBE = """
await until(() => document.querySelector('.provider-row'));
await openSettings('sources');
await wait(80);
const out = {};

// 1. Open Add profile via "+ Add another profile" on claude_local
document.getElementById('addProfBtn_claude_local').click();
await wait(40);
const radios = () => [...document.querySelectorAll('input[name="addChoice_claude_local"]')];
out.openedFromBtn = {
  radios: radios().map(r => ({ value: r.value, checked: r.checked })),
  suggestedName: document.getElementById('addName_claude_local').value,
  customPathVisibleBefore: !!document.getElementById('addPath_claude_local'),
};
document.querySelector('.add-profile-panel .btn-secondary').click();
await wait(30);
out.closedOnCancel = !document.querySelector('.add-profile-panel');

// 2. Open via "Add" button on detected candidate
document.querySelector('button[data-candidate="cand-fresh"]').click();
await wait(40);
out.openedFromCandidate = {
  checkedValue: (radios().find(r => r.checked) || {}).value,
  suggestedName: document.getElementById('addName_claude_local').value,
};
const nameInp = document.getElementById('addName_claude_local');
nameInp.value = 'Side Project';
nameInp.dispatchEvent(new Event('input', { bubbles: true }));
document.querySelector('.add-profile-panel').requestSubmit();
await until(() => window.__calls.some(c => c[0] === 'add_profile'));
await wait(40);
out.afterCandSubmit = {
  panelOpen: !!document.querySelector('.add-profile-panel'),
  toast: document.getElementById('toast').textContent,
  call: window.__calls.find(c => c[0] === 'add_profile'),
};

// 3. Reopen, select "Another folder…", verify Browse & custom folder submit
document.getElementById('addProfBtn_claude_local').click();
await wait(40);
const customRadio = document.querySelector('input[name="addChoice_claude_local"][value="__custom__"]');
customRadio.checked = true;
customRadio.dispatchEvent(new Event('change', { bubbles: true }));
await wait(40);
out.customRevealed = {
  pathVisible: !!document.getElementById('addPath_claude_local'),
  browseBtn: !!document.querySelector('.add-profile-panel button[data-source="claude_local"]'),
};
// Submitting empty custom path should show validation toast without calling API
document.querySelector('.add-profile-panel').requestSubmit();
await wait(20);
out.emptyCustomCalls = window.__calls.filter(c => c[0] === 'add_profile').length;
out.emptyCustomToast = document.getElementById('toast').textContent;

// Click Browse... to populate location + auto-suggested name
document.querySelector('.add-profile-panel button[data-source="claude_local"]').click();
await wait(40);
out.afterBrowse = {
  pathVal: document.getElementById('addPath_claude_local').value,
  nameVal: document.getElementById('addName_claude_local').value,
};
const customName = document.getElementById('addName_claude_local');
customName.value = 'Client Work';
customName.dispatchEvent(new Event('input', { bubbles: true }));
document.querySelector('.add-profile-panel').requestSubmit();
await until(() => window.__calls.filter(c => c[0] === 'add_profile').length === 2);
await wait(40);
out.customSubmitCall = window.__calls.filter(c => c[0] === 'add_profile')[1];
report(out);
"""


@needs_chrome
@browser
def test_add_profile_inline_editor_flows(tmp_path: Path):
    html = build_page(
        pref="light",
        probe=ADD_PROFILE_FLOW_PROBE,
        settings=p6_settings_fixture(),
        api_js=_p6_api_js(),
        init={"demo": False},
        extra_css=FAST,
    )
    r = probe(html, 380, tmp_path, height=900)
    # Duplicate candidate cand-dup must NOT appear as a radio option
    assert r["openedFromBtn"]["radios"] == [
        {"value": "cand-fresh", "checked": True},
        {"value": "__custom__", "checked": False},
    ]
    assert r["openedFromBtn"]["suggestedName"] == "Fresh"
    assert r["openedFromBtn"]["customPathVisibleBefore"] is False
    assert r["closedOnCancel"] is True

    assert r["openedFromCandidate"] == {
        "checkedValue": "cand-fresh",
        "suggestedName": "Fresh",
    }
    assert r["afterCandSubmit"] == {
        "panelOpen": False,
        "toast": "Profile added",
        "call": ["add_profile", "claude_local", "cand-fresh", "", "Side Project"],
    }

    assert r["customRevealed"] == {"pathVisible": True, "browseBtn": True}
    assert r["emptyCustomCalls"] == 1
    assert "folder" in r["emptyCustomToast"].lower()
    assert r["afterBrowse"] == {
        "pathVal": "C:/Users/me/.claude-custom",
        "nameVal": "Custom",
    }
    assert r["customSubmitCall"] == [
        "add_profile",
        "claude_local",
        "",
        "C:/Users/me/.claude-custom",
        "Client Work",
    ]


CONFIGURE_PROFILE_FLOW_PROBE = """
await until(() => document.querySelector('.provider-row'));
await openSettings('sources');
await wait(80);
const out = {};

// 1. Default profile: Stop tracking label, path edit + Browse + Test & connect
document.getElementById('cfg_claude_local').click();
await wait(40);
out.defaultRemoveLabel = document.querySelector('.inline-editor .btn-danger-quiet').textContent.trim();
document.querySelector('.inline-editor button[data-editor="claude_local"]').click();
await wait(40);
out.defaultPathAfterBrowse = document.getElementById('srcInput_claude_local').value;
document.getElementById('srcInput_claude_local').closest('form').requestSubmit();
await until(() => window.__calls.some(c => c[0] === 'connect_source'));
await wait(40);
out.connectCall = window.__calls.find(c => c[0] === 'connect_source');

// 2. Stop tracking on default profile -> remove_profile(source, "default", revision)
document.getElementById('cfg_claude_local').click();
await wait(40);
document.querySelector('.inline-editor .btn-danger-quiet').click();
await until(() => window.__calls.some(c => c[0] === 'remove_profile'));
await wait(40);
out.stopTrackingCall = window.__calls.find(c => c[0] === 'remove_profile');
out.stopTrackingToast = document.getElementById('toast').textContent;

// 3. Secondary profile: Rename + Remove label + connect_source with profile key
document.getElementById('cfg_claude_local__claude-code-work').click();
await wait(40);
out.workRemoveLabel = document.querySelector('.inline-editor .btn-danger-quiet').textContent.trim();
const workPath = document.getElementById('srcInput_claude_local__claude-code-work');
workPath.value = 'D:/Work/.claude';
workPath.closest('form').requestSubmit();
await until(() => window.__calls.filter(c => c[0] === 'connect_source').length === 2);
await wait(40);
out.workConnectCall = window.__calls.filter(c => c[0] === 'connect_source')[1];

// Reopen to test Rename
document.getElementById('cfg_claude_local__claude-code-work').click();
await wait(40);
const renameInp = document.getElementById('profName_claude_local__claude-code-work');
renameInp.value = '   ';
renameInp.closest('form').requestSubmit();
await wait(20);
out.emptyRenameCalls = window.__calls.filter(c => c[0] === 'rename_profile').length;
renameInp.value = 'Client Alpha';
renameInp.closest('form').requestSubmit();
await until(() => window.__calls.some(c => c[0] === 'rename_profile'));
await wait(40);
out.renameCall = window.__calls.find(c => c[0] === 'rename_profile');
out.renameToast = document.getElementById('toast').textContent;

// Reopen to test Remove on non-default profile
document.getElementById('cfg_claude_local__claude-code-work').click();
await wait(40);
document.querySelector('.inline-editor .btn-danger-quiet').click();
await until(() => window.__calls.filter(c => c[0] === 'remove_profile').length === 2);
await wait(40);
out.workRemoveCall = window.__calls.filter(c => c[0] === 'remove_profile')[1];
out.workRemoveToast = document.getElementById('toast').textContent;

// 4. Dismiss candidate
document.querySelector('button[data-candidate="cand-fresh"][onclick*="dismissCandidate"]').click();
await until(() => window.__calls.some(c => c[0] === 'dismiss_candidate'));
await wait(30);
out.dismissCall = window.__calls.find(c => c[0] === 'dismiss_candidate');
out.dismissToast = document.getElementById('toast').textContent;

// 5. A missing default location can still be stopped when its old readings remain visible.
document.getElementById('cfg_codex_local').click();
await wait(40);
const stopDefault = document.querySelector('.inline-editor .btn-danger-quiet');
out.notFoundEditor = {
  hasPathInput: !!document.getElementById('srcInput_codex_local'),
  hasRename: !!document.getElementById('profName_codex_local'),
  stopLabel: stopDefault?.textContent.trim(),
};
stopDefault?.click();
await until(() => window.__calls.some(c => c[0] === 'remove_profile' && c[1] === 'codex_local'));
out.stopDefaultCall = window.__calls.find(c => c[0] === 'remove_profile' && c[1] === 'codex_local');
report(out);
"""


@needs_chrome
@browser
def test_profile_configure_rename_remove_and_dismiss_flows(tmp_path: Path):
    html = build_page(
        pref="light",
        probe=CONFIGURE_PROFILE_FLOW_PROBE,
        settings=p6_settings_fixture(),
        api_js=_p6_api_js(),
        init={"demo": False},
        extra_css=FAST,
    )
    r = probe(html, 380, tmp_path, height=900)
    assert r["defaultRemoveLabel"] == "Stop tracking"
    assert r["defaultPathAfterBrowse"] == "C:/Users/me/.claude-custom"
    assert r["connectCall"] == [
        "connect_source",
        "claude_local",
        False,
        "C:/Users/me/.claude-custom",
        4,
        "default",
    ]
    assert r["stopTrackingCall"] == ["remove_profile", "claude_local", "default", 4]
    assert r["stopTrackingToast"] == "Stopped tracking profile"

    assert r["workRemoveLabel"] == "Remove"
    assert r["workConnectCall"] == [
        "connect_source",
        "claude_local",
        False,
        "D:/Work/.claude",
        2,
        "claude-code-work",
    ]
    assert r["emptyRenameCalls"] == 0
    assert r["renameCall"] == [
        "rename_profile",
        "claude_local",
        "claude-code-work",
        "Client Alpha",
        2,
    ]
    assert r["renameToast"] == "Profile renamed"
    assert r["workRemoveCall"] == [
        "remove_profile",
        "claude_local",
        "claude-code-work",
        2,
    ]
    assert r["workRemoveToast"] == "Profile removed"
    assert r["dismissCall"] == ["dismiss_candidate", "claude_local", "cand-fresh"]
    assert r["dismissToast"] == "Candidate dismissed"
    assert r["notFoundEditor"] == {
        "hasPathInput": True,
        "hasRename": False,
        "stopLabel": "Stop tracking",
    }
    assert r["stopDefaultCall"] == ["remove_profile", "codex_local", "default", 1]


P6_ESCAPE_PROBE = """
await until(() => document.querySelector('.provider-row'));
const escKey = () => (document.activeElement || document.body).dispatchEvent(
  new KeyboardEvent('keydown', { key: 'Escape', bubbles: true })
);
await openSettings('sources');
await wait(80);
const out = {};

// 1. Add profile editor: Escape closes panel and restores focus to #addProfBtn_<src>
document.getElementById('addProfBtn_claude_local').click();
await wait(40);
document.getElementById('addName_claude_local').focus();
out.addOpen = !!document.querySelector('.add-profile-panel');
escKey();
await wait(40);
out.afterAddEsc = {
  addOpen: !!document.querySelector('.add-profile-panel'),
  sheetOpen: document.getElementById('settingsSheet').classList.contains('open'),
  focusId: document.activeElement.id,
};

// 2. Non-default profile Configure editor: Escape closes editor and focuses #cfg_<edKey>
document.getElementById('cfg_claude_local__claude-code-work').click();
await wait(40);
document.getElementById('srcInput_claude_local__claude-code-work').focus();
out.workEditOpen = !!document.querySelector('.inline-editor');
escKey();
await wait(40);
out.afterWorkEditEsc = {
  editOpen: !!document.querySelector('.inline-editor'),
  sheetOpen: document.getElementById('settingsSheet').classList.contains('open'),
  focusId: document.activeElement.id,
};

// 3. Default profile Configure editor: Escape focuses #cfg_<source>
document.getElementById('cfg_claude_local').click();
await wait(40);
document.getElementById('profName_claude_local').focus();
escKey();
await wait(40);
out.afterDefaultEditEsc = {
  editOpen: !!document.querySelector('.inline-editor'),
  sheetOpen: document.getElementById('settingsSheet').classList.contains('open'),
  focusId: document.activeElement.id,
};

// 4. Next Escape closes the settings sheet itself
escKey();
await wait(40);
out.afterSheetEsc = {
  sheetOpen: document.getElementById('settingsSheet').classList.contains('open'),
  focusId: document.activeElement.id,
};
report(out);
"""


@needs_chrome
@browser
def test_escape_closes_add_profile_and_profile_configure_editors_and_restores_focus(
    tmp_path: Path,
):
    html = build_page(
        pref="light",
        probe=P6_ESCAPE_PROBE,
        settings=p6_settings_fixture(),
        init={"demo": False},
        extra_css=FAST,
    )
    r = probe(html, 380, tmp_path, height=900)
    assert r["addOpen"] is True
    assert r["afterAddEsc"] == {
        "addOpen": False,
        "sheetOpen": True,
        "focusId": "addProfBtn_claude_local",
    }
    assert r["workEditOpen"] is True
    assert r["afterWorkEditEsc"] == {
        "editOpen": False,
        "sheetOpen": True,
        "focusId": "cfg_claude_local__claude-code-work",
    }
    assert r["afterDefaultEditEsc"] == {
        "editOpen": False,
        "sheetOpen": True,
        "focusId": "cfg_claude_local",
    }
    assert r["afterSheetEsc"] == {
        "sheetOpen": False,
        "focusId": "settingsBtn",
    }


def _xss_settings_payload() -> dict:
    p_img = "<img src=x onerror=window.__xss=1>"
    p_script = "<script>window.__xss=2</script>"
    p_quotes = """'\" onclick="window.__xss=3" data-x='"""
    return {
        "ok": True,
        "connections": [
            {
                "source": "claude_local",
                "name": f"Claude {p_img} {p_quotes}",
                "kind": "local",
                "state": "receiving",
                "supportsProfiles": True,
                "revision": 5,
                "profiles": [
                    {
                        "key": "default",
                        "name": f"Main {p_script} {p_quotes}",
                        "location": f"C:/Users/{p_img}/.claude",
                        "origin": "default",
                        "state": "receiving",
                        "revision": 5,
                        "plan": f"Plan {p_img}",
                        "sharesLimitsWith": None,
                    },
                    {
                        "key": f"claude-code-{p_quotes}",
                        "name": f"Sub {p_img}",
                        "location": f"C:/Users/{p_quotes}/.claude-sub",
                        "origin": "manual",
                        "state": "added",
                        "revision": 7,
                        "plan": f"Pro {p_script}",
                        "sharesLimitsWith": "default",
                    },
                    {
                        "key": "claude-code-orphan",
                        "name": "Orphan",
                        "location": "C:/Users/me/.claude-orphan",
                        "origin": "manual",
                        "state": "added",
                        "revision": 8,
                        "plan": "Pro",
                        "sharesLimitsWith": f"missing-{p_img}-{p_quotes}",
                    },
                ],
                "detected": [
                    {
                        "candidateId": f"cand-{p_quotes}",
                        "folder": f"~/.claude-{p_img}",
                        "plan": f"Plan {p_script}",
                        "sameAccountAs": None,
                    },
                    {
                        "candidateId": "cand-dup-xss",
                        "folder": f"~/.claude-dup-{p_quotes}",
                        "plan": f"Plan {p_img}",
                        "sameAccountAs": "default",
                    },
                    {
                        "candidateId": "cand-unmatched-xss",
                        "folder": "~/.claude-raw-target",
                        "plan": "Pro",
                        "sameAccountAs": f"raw-{p_img}-{p_quotes}",
                    },
                ],
            }
        ],
        "machines": [],
        "setup": {},
        "hubAvailable": False,
    }


XSS_PROBE = """
await until(() => document.querySelector('.provider-row'));
await openSettings('sources');
await wait(80);
const sheet = document.getElementById('sheetBody');
const checkDomClean = () => ({
  xssFlag: window.__xss === undefined ? null : window.__xss,
  injectedImgs: sheet.querySelectorAll('img').length,
  injectedScripts: sheet.querySelectorAll('script').length,
  providerImgs: document.getElementById('providerList').querySelectorAll('img').length,
  providerScripts: document.getElementById('providerList').querySelectorAll('script').length,
});

const text = sheet.textContent;
const pImg = '<img src=x onerror=window.__xss=1>';
const pScript = '<script>window.__xss=2<\\/script>';
const pQuotes = `'\" onclick="window.__xss=3" data-x='`;
const out = {
  initial: checkDomClean(),
  renderedLiterals: {
    providerName: text.includes('Claude ' + pImg),
    profileName: text.includes('Main ' + pScript),
    profileLocation: text.includes('C:/Users/' + pImg + '/.claude'),
    profilePlan: text.includes('Plan ' + pImg),
    detectedFolder: text.includes('~/.claude-' + pImg),
    sharesTargetName: text.includes('Shares limits with Main ' + pScript),
    sharesMissingRaw: text.includes('Shares limits with missing-' + pImg),
    sameTargetName: text.includes('Same account as Main ' + pScript),
    sameMissingRaw: text.includes('Same account as raw-' + pImg),
    quotesPreserved: text.includes(pQuotes),
  },
};

// Interactive clicks & submits still work with quoted/HTML keys
// 1. Open Add profile via button, select detected radio, and submit
document.getElementById('addProfBtn_claude_local').click();
await wait(40);
out.inAddPanel = checkDomClean();
out.addPanelLiteralFolder = document.querySelector('.add-profile-panel').textContent.includes('~/.claude-' + pImg);
const candRadio = sheet.querySelector('input[type="radio"]');
candRadio.click();
await wait(20);
document.querySelector('.add-profile-panel').requestSubmit();
await until(() => window.__calls.some(c => c[0] === 'add_profile'));
await wait(30);
out.addCall = window.__calls.find(c => c[0] === 'add_profile');

// 2. Configure the profile whose key contains single & double quotes: Rename + Remove
const subCfg = [...sheet.querySelectorAll('button[id^="cfg_claude_local__"]')].find(
  b => b.dataset.profile && b.dataset.profile.includes('onclick=')
);
subCfg.click();
await wait(40);
out.inConfigure = checkDomClean();
const renameForm = [...sheet.querySelectorAll('.inline-editor form')][1];
renameForm.querySelector('input').value = 'Safe Name';
renameForm.requestSubmit();
await until(() => window.__calls.some(c => c[0] === 'rename_profile'));
await wait(30);
out.renameCall = window.__calls.find(c => c[0] === 'rename_profile');

subCfg.click();
await wait(40);
sheet.querySelector('.inline-editor .btn-danger-quiet').click();
await until(() => window.__calls.some(c => c[0] === 'remove_profile'));
await wait(30);
out.removeCall = window.__calls.find(c => c[0] === 'remove_profile');

// 3. Dismiss the candidate whose candidateId contains quotes
const dismissBtn = [...sheet.querySelectorAll('button')].find(
  b => b.textContent.trim() === 'Dismiss' && b.dataset.candidate && b.dataset.candidate.includes('onclick=')
);
dismissBtn.click();
await until(() => window.__calls.some(c => c[0] === 'dismiss_candidate'));
await wait(30);
out.dismissCall = window.__calls.find(c => c[0] === 'dismiss_candidate');

// 4. Fallback getNormalizedProviders renders escaped tool/profile names on main card
renderReport({
  total: { valueUsd: 1.5, tokens: 100, events: 1, complete: true },
  providers: [],
  tools: [
    {
      key: sheet.querySelector('button[id^="cfg_claude_local__"]').dataset.profile,
      name: 'Fallback ' + pImg,
      tokens: 100,
      valueUsd: 1.5,
      complete: true,
      models: [{ key: 'm1', name: 'Model ' + pImg, tokens: 100, valueUsd: 1.5 }],
    }
  ],
  machines: [],
}, false);
await wait(40);
out.fallbackMain = checkDomClean();
const cardText = document.getElementById('providerList').textContent;
out.fallbackLiteral = cardText.includes('Claude ' + pImg) && cardText.includes('Sub ' + pImg) && cardText.includes('Model ' + pImg);
report(out);
"""


@needs_chrome
@browser
def test_p6_xss_escaping_and_quoted_identifiers_work_safely(tmp_path: Path):
    # Escape "</" before embedding JSON in the <script> test stub so the HTML parser
    # does not close the stub <script> tag early; JS still receives literal "</script>".
    safe_settings_js = json.dumps(_xss_settings_payload()).replace("</", "<\\/")
    api_js = _p6_api_js() + f"\napi.get_settings_data = async function () {{ return {safe_settings_js}; }};"
    html = build_page(
        pref="light",
        probe=XSS_PROBE,
        api_js=api_js,
        init={"demo": False},
        extra_css=FAST,
    )
    r = probe(html, 380, tmp_path, height=900)
    for stage in ("initial", "inAddPanel", "inConfigure", "fallbackMain"):
        assert r[stage] == {
            "xssFlag": None,
            "injectedImgs": 0,
            "injectedScripts": 0,
            "providerImgs": 0,
            "providerScripts": 0,
        }, (stage, r[stage])

    assert all(r["renderedLiterals"].values()), r["renderedLiterals"]
    assert r["addPanelLiteralFolder"] is True
    assert r["fallbackLiteral"] is True

    # Interactive actions still passed exact decoded IDs through dataset attributes
    assert r["addCall"][0:3] == [
        "add_profile",
        "claude_local",
        """cand-'" onclick="window.__xss=3" data-x='""",
    ]
    assert r["renameCall"] == [
        "rename_profile",
        "claude_local",
        """claude-code-'" onclick="window.__xss=3" data-x='""",
        "Safe Name",
        7,
    ]
    assert r["removeCall"] == [
        "remove_profile",
        "claude_local",
        """claude-code-'" onclick="window.__xss=3" data-x='""",
        7,
    ]
    assert r["dismissCall"] == [
        "dismiss_candidate",
        "claude_local",
        """cand-'" onclick="window.__xss=3" data-x='""",
    ]


FALLBACK_AND_FONT_PROBE = """
await until(() => document.querySelector('.provider-row'));
await openSettings('sources');
await wait(80);
const out = {};
out.sourcesSmall = smallText();

// Open Add profile panel and switch to Another folder... so path + name + radios are all visible
document.getElementById('addProfBtn_claude_local').click();
await wait(40);
const customRadio = document.querySelector('input[name="addChoice_claude_local"][value="__custom__"]');
customRadio.checked = true;
customRadio.dispatchEvent(new Event('change', { bubbles: true }));
await wait(40);
out.addPanelSmall = smallText();

// Open profile Configure editor on non-default profile
document.getElementById('cfg_claude_local__claude-code-work').click();
await wait(40);
out.configureSmall = smallText();

// Fallback getNormalizedProviders when report.providers is empty
const fallbackProviders = getNormalizedProviders({
  providers: [],
  tools: [
    { key: 'claude-code-work', name: 'claude-code-work', tokens: 500, valueUsd: 2.5, complete: true, models: [] },
    { key: 'codex-alt', name: 'Codex · Client B', tokens: 300, valueUsd: 1.25, complete: true, models: [] },
  ],
});
out.fallback = fallbackProviders.map(p => ({
  key: p.key,
  name: p.name,
  badge: p.badge,
  plan: p.plan,
}));
report(out);
"""


@needs_chrome
@browser
@pytest.mark.parametrize("pref", ["light", "dark"])
def test_p6_sources_add_and_configure_have_no_small_text_and_fallback_resolves_profiles(
    tmp_path: Path, pref: str
):
    html = build_page(
        pref=pref,
        probe=FALLBACK_AND_FONT_PROBE,
        settings=p6_settings_fixture(),
        init={"demo": False},
        extra_css=FAST,
    )
    r = probe(html, 380, tmp_path, height=900)
    assert r["sourcesSmall"] == []
    assert r["addPanelSmall"] == []
    assert r["configureSmall"] == []
    assert r["fallback"] == [
        {
            "key": "claude-code-work",
            "name": "Claude Code",
            "badge": "Work",
            "plan": "Claude Pro",
        },
        {
            "key": "codex-alt",
            "name": "Codex",
            "badge": "Client B",
            "plan": "Recorded usage",
        },
    ]
