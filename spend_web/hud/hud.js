      (() => {
        const $ = id => document.getElementById(id);
        const root = document.documentElement;
        // The full dashboard is optional (BURNRATE_ENABLE_DASHBOARD=1).
        const dashboardOn = root.dataset.dashboard !== "off";
        const glass = root.classList.contains("is-glass");
        // True while the card animates between the full and mini layouts.
        let morphing = false;
        let selectedMachine = new URLSearchParams(location.search).get("machine") || "all";
        let pinned = true;
        try {
          const savedPin = localStorage.getItem("burnrate:hud-pinned");
          if (savedPin === "0") pinned = false;
        } catch {}

        const usd = v => {
          const n = v == null || v === "" ? NaN : Number(v);
          if (!Number.isFinite(n)) return "—";
          return "$" + Math.round(n).toLocaleString("en-US");
        };
        const usd2 = v => {
          const n = v == null || v === "" ? NaN : Number(v);
          if (!Number.isFinite(n)) return "—";
          return "$" + n.toLocaleString("en-US", {minimumFractionDigits: 2, maximumFractionDigits: 2});
        };
        // A balance short enough for the meter's value column; the tip shows cents.
        const usdShort = v => {
          const n = v == null || v === "" ? NaN : Number(v);
          if (!Number.isFinite(n)) return "—";
          if (n >= 1000) return "$" + (n / 1000).toFixed(1) + "K";
          if (n >= 10) return "$" + Math.round(n);
          return "$" + n.toFixed(2);
        };
        const tok = v => {
          const n = v == null || v === "" ? NaN : Number(v);
          if (!Number.isFinite(n)) return "—";
          if (n >= 1e9) return (n / 1e9).toFixed(2) + "B";
          if (n >= 1e6) return (n / 1e6).toFixed(2) + "M";
          if (n >= 1e3) return (n / 1e3).toFixed(1) + "K";
          return String(Math.round(n));
        };
        const pctText = value => value == null ? "—" : (value > 0 && value < 1 ? "<1%" : Math.round(value) + "%");
        const esc = value => String(value ?? "").replace(/[&<>"']/g, ch => (
          {"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[ch]
        ));
        const fmtNum = v => {
          const n = Number(v);
          if (!Number.isFinite(n)) return "—";
          if (Math.abs(n) >= 100) return Math.round(n).toLocaleString("en-US");
          if (Math.abs(n) >= 10) return String(Math.round(n * 10) / 10);
          return String(Math.round(n * 100) / 100);
        };
        const qtyText = (value, unit) => {
          const n = Number(value);
          if (!Number.isFinite(n)) return null;
          const u = String(unit || "");
          if (u === "usd") return usd2(n);
          if (u === "pct" || u === "percent") return Math.round(n) + "%";
          if (u === "credits") return fmtNum(n) + " credits";
          if (u === "unavailable" || u === "") return fmtNum(n);
          return fmtNum(n) + " " + u;
        };
        const tok1 = v => {
          const n = v == null || v === "" ? NaN : Number(v);
          if (!Number.isFinite(n)) return "—";
          const a = Math.abs(n);
          if (a >= 1e9) return (n / 1e9).toFixed(a >= 1e10 ? 1 : 2) + "B";
          if (a >= 1e6) return (n / 1e6).toFixed(a >= 1e8 ? 0 : 1) + "M";
          if (a >= 1e3) return (n / 1e3).toFixed(a >= 1e5 ? 0 : 1) + "K";
          return String(Math.round(n));
        };

        // Tooltip markup. Every builder escapes its own text.
        const T = {
          head: (title, sub) => '<div class="tt-head"><b>' + esc(title) + "</b>" + (sub ? "<span>" + esc(sub) + "</span>" : "") + "</div>",
          hero: (big, strong, side) => '<div class="tt-hero"><strong>' + esc(big) + "</strong><span>" +
            (strong ? "<b>" + esc(strong) + "</b>" : "") + esc(side || "") + "</span></div>",
          kv: rows => {
            const body = rows.filter(Boolean).map(([key, value, cls, note]) =>
              "<dt>" + esc(key) + "</dt><dd" + (cls ? ' class="' + cls + '"' : "") + ">" + esc(value) +
              (note ? "<small>" + esc(note) + "</small>" : "") + "</dd>").join("");
            return body ? '<dl class="tt-kv">' + body + "</dl>" : "";
          },
          line: (text, cls) => text ? '<p class="tt-line' + (cls ? " " + cls : "") + '">' + esc(text) + "</p>" : "",
          section: (label, value) => '<div class="tt-section"><span>' + esc(label) + "</span>" + (value ? "<b>" + esc(value) + "</b>" : "") + "</div>",
          sep: '<hr class="tt-sep">',
          foot: text => text ? '<p class="tt-foot">' + esc(text) + "</p>" : ""
        };

        // An element's tip is built when shown (and rebuilt on every data
        // refresh while open), so an open tip always shows current values.
        const tipContent = new WeakMap();
        function setTip(el, content, kind = "wide") {
          if (!el) return;
          tipContent.set(el, {content, kind});
          el.setAttribute("data-tip", "");
        }
        const labelTip = (el, text) => setTip(el, text, "label");

        const tipEl = $("hud-tip");
        const TIP_DELAY_MS = 320;
        const TIP_WARM_MS = 600;
        const tip = {target: null, pending: null, timer: 0, leaveTimer: 0, restoreTimer: 0, hiddenAt: 0, x: -1, y: -1, inside: false, grown: false};
        const tipUnderPointer = () => {
          if (!tip.inside || tip.x < 0) return null;
          const el = document.elementFromPoint(tip.x, tip.y);
          return el ? el.closest("[data-tip]") : null;
        };
        function placeTip(target) {
          const rect = target.getBoundingClientRect();
          const tw = tipEl.offsetWidth;
          const th = tipEl.offsetHeight;
          const x = Math.min(Math.max(6, rect.left + rect.width / 2 - tw / 2), Math.max(6, window.innerWidth - tw - 6));
          let y = rect.top - th - 6;
          if (y < 6) {
            y = rect.bottom + 6;
            const bottom = y + th + 6;
            if (bottom > window.innerHeight) {
              // Glass: the window is transparent past the card, so it can
              // grow downward for a tall tip instead of clipping it.
              if (glass && !morphing) growWindowForTip(bottom);
              else y = Math.max(6, window.innerHeight - th - 6);
            }
          }
          tipEl.style.left = Math.round(x) + "px";
          tipEl.style.top = Math.round(y) + "px";
        }
        function showTip(target, snap) {
          const entry = target && target.isConnected ? tipContent.get(target) : null;
          const value = entry ? (typeof entry.content === "function" ? entry.content() : entry.content) : "";
          if (!value) { hideTip(); return; }
          clearTimeout(tip.leaveTimer);
          clearTimeout(tip.restoreTimer);
          tip.target = target;
          tipEl.className = "is-" + entry.kind + (snap ? " snap" : "");
          tipEl.innerHTML = entry.kind === "label" ? esc(value) : value;
          placeTip(target);
          tipEl.classList.add("on");
          tipEl.setAttribute("aria-hidden", "false");
        }
        function hideTip() {
          clearTimeout(tip.timer);
          clearTimeout(tip.leaveTimer);
          tip.pending = null;
          if (tip.target) tip.hiddenAt = Date.now();
          tip.target = null;
          tipEl.classList.remove("on", "snap");
          tipEl.setAttribute("aria-hidden", "true");
          if (tip.grown) {
            clearTimeout(tip.restoreTimer);
            tip.restoreTimer = setTimeout(() => {
              if (tip.target) return;
              tip.grown = false;
              syncHudWindowSize(true);
            }, 160);
          }
        }
        function wantTip(target) {
          if (morphing) { hideTip(); return; }
          if (target && (target === tip.target || target === tip.pending)) { clearTimeout(tip.leaveTimer); return; }
          clearTimeout(tip.timer);
          tip.pending = null;
          if (!target) {
            // A short grace so crossing the gap between two rows moves the
            // open tip instead of closing and reopening it.
            if (tip.target) { clearTimeout(tip.leaveTimer); tip.leaveTimer = setTimeout(hideTip, 90); }
            return;
          }
          if (tip.target || Date.now() - tip.hiddenAt < TIP_WARM_MS) { showTip(target, Boolean(tip.target)); return; }
          tip.pending = target;
          tip.timer = setTimeout(() => {
            tip.pending = null;
            const live = target.isConnected ? target : tipUnderPointer();
            if (live && tip.inside) showTip(live, false);
          }, TIP_DELAY_MS);
        }
        // After a re-render the hovered element may be a new node: follow the
        // pointer to it, or close the tip when nothing tipped is under it.
        function refreshTip() {
          if (!tip.target) return;
          const target = tip.target.isConnected && tip.target.matches(":hover") ? tip.target : tipUnderPointer();
          if (target) showTip(target, true); else hideTip();
        }
        document.addEventListener("pointermove", event => { tip.x = event.clientX; tip.y = event.clientY; tip.inside = true; }, {passive: true});
        document.addEventListener("pointerover", event => {
          tip.inside = true;
          tip.x = event.clientX;
          tip.y = event.clientY;
          wantTip(event.target instanceof Element ? event.target.closest("[data-tip]") : null);
        });
        const leaveWindow = () => { tip.inside = false; hideTip(); };
        document.addEventListener("pointerout", event => { if (!event.relatedTarget) leaveWindow(); });
        document.documentElement.addEventListener("pointerleave", leaveWindow);
        document.addEventListener("pointerdown", hideTip, true);
        window.addEventListener("wheel", hideTip, {passive: true});
        window.addEventListener("scroll", hideTip, true);
        window.addEventListener("blur", hideTip);
        document.addEventListener("visibilitychange", () => { if (document.hidden) hideTip(); });
        // Last line of defence against a tip left behind by a missed event.
        setInterval(() => { if (tip.target && !(tip.target.isConnected && tip.target.matches(":hover"))) refreshTip(); }, 700);

        function setStat(id, text) {
          const el = $(id);
          if (!el || el.textContent === text) return;
          const first = el.textContent === "—";
          el.textContent = text;
          if (first) return;
          el.classList.remove("tick");
          void el.offsetWidth;
          el.classList.add("tick");
        }

        async function tauriInvoke(cmd, args = {}) {
          const internals = window.__TAURI_INTERNALS__;
          if (internals && typeof internals.invoke === "function") {
            try {
              return await internals.invoke(cmd, args);
            } catch {
              return null;
            }
          }
          return null;
        }

        let miniBarDragged = false;
        document.addEventListener("mousedown", event => {
          if (event.button !== 0 || event.detail > 1) return;
          if (event.target.closest("button, a, input, select, textarea, [role='radio'], .hud-more-menu")) return;
          const miniBar = event.target.closest("#hud-mini-bar");
          if (miniBar) {
            const startX = event.clientX, startY = event.clientY;
            const onMove = moveEvent => {
              if (Math.hypot(moveEvent.clientX - startX, moveEvent.clientY - startY) > 4) {
                miniBarDragged = true;
                cleanup();
                tauriInvoke("start_hud_drag");
              }
            };
            const cleanup = () => {
              window.removeEventListener("mousemove", onMove);
              window.removeEventListener("mouseup", cleanup);
            };
            window.addEventListener("mousemove", onMove);
            window.addEventListener("mouseup", cleanup, {once: true});
            return;
          }
          if (!event.target.closest(".top")) return;
          tauriInvoke("start_hud_drag");
        });

        function applyPinUi(stateVal) {
          pinned = Boolean(stateVal);
          try { localStorage.setItem("burnrate:hud-pinned", pinned ? "1" : "0"); } catch {}
          const btn = $("hud-pin-btn");
          if (btn) {
            btn.classList.toggle("active", pinned);
            btn.setAttribute("aria-pressed", String(pinned));
          }
        }
        applyPinUi(pinned);
        tauriInvoke("set_hud_pin", { pinned });

        function closeMoreMenu(restoreFocus = false) {
          const menu = $("hud-more-menu"), btn = $("hud-more-btn");
          if (!menu || menu.hidden) return;
          menu.hidden = true;
          btn?.setAttribute("aria-expanded", "false");
          if (restoreFocus) btn?.focus({preventScroll: true});
        }
        $("hud-more-btn")?.addEventListener("click", event => {
          event.stopPropagation();
          const menu = $("hud-more-menu"), btn = $("hud-more-btn");
          if (!menu) return;
          const nextOpen = menu.hidden;
          menu.hidden = !nextOpen;
          btn?.setAttribute("aria-expanded", String(nextOpen));
          hideTip();
          if (nextOpen) menu.querySelector("button:not([hidden])")?.focus({preventScroll: true});
        });
        document.addEventListener("pointerdown", event => {
          const wrap = $("hud-more-wrap");
          if (wrap && !wrap.contains(event.target)) closeMoreMenu(false);
        });

        // An installed app checks for updates in the background and offers one here.
        function showUpdate(version) {
          const bar = $("hud-update");
          if (!bar || !version || bar.disabled) return;
          bar.textContent = "BURNRATE " + version + " is ready \u00b7 install and restart";
          bar.hidden = false;
        }
        $("hud-update").addEventListener("click", async () => {
          const bar = $("hud-update");
          bar.disabled = true;
          bar.textContent = "Installing the update\u2026";
          // A successful install never answers: the app closes for the installer or restarts.
          let reason = "";
          try {
            await window.__TAURI_INTERNALS__?.invoke?.("install_update");
          } catch (error) {
            reason = String(error?.message ?? error ?? "").trim();
          }
          if (reason.length > 120) reason = reason.slice(0, 119) + "\u2026";
          bar.disabled = false;
          bar.textContent = "The update did not install" + (reason ? " (" + reason + ")" : "") + " \u00b7 try again";
        });
        tauriInvoke("pending_update").then(showUpdate);
        window.__TAURI__?.event?.listen?.("burnrate://update-available", event => showUpdate(event.payload));
        labelTip($("hud-pin-btn"), () => pinned ? "Unpin from top" : "Keep on top");
        labelTip($("hud-mini-btn"), () => isMini ? "Expand" : "Collapse");
        labelTip($("hud-more-btn"), "More actions");
        $("hud-share-btn").addEventListener("click", () => { closeMoreMenu(false); shareWeek(); });
        // Setup is reachable from the HUD even with the dashboard off: the
        // desktop app opens it in its main window, a browser in a new tab.
        $("hud-setup-btn").addEventListener("click", async () => {
          closeMoreMenu(false);
          const internals = window.__TAURI_INTERNALS__;
          if (internals && typeof internals.invoke === "function") {
            try { await internals.invoke("open_setup_wizard"); return; } catch {}
          }
          window.open("/setup", "burnrate-setup");
        });
        labelTip($("hud-hide-btn"), "Hide \u00b7 reopen from the tray");
        labelTip($("hud-poll-btn"), "Poll every plan now");

        $("hud-pin-btn").addEventListener("click", async () => {
          const nextPin = !pinned;
          const res = await tauriInvoke("set_hud_pin", { pinned: nextPin });
          applyPinUi(typeof res === "boolean" ? res : nextPin);
        });

        $("hud-expand-btn").addEventListener("click", async () => {
          closeMoreMenu(false);
          const handled = await tauriInvoke("show_dashboard", { path: "/" });
          if (handled === null) window.open("/", "_blank");
        });

        $("hud-hide-btn").addEventListener("click", async () => {
          const handled = await tauriInvoke("hide_hud");
          if (handled === null) window.close();
        });

        async function openToolInDashboard(toolKey) {
          if (!dashboardOn) return;
          const clean = String(toolKey || "").trim();
          try {
            if (clean) {
              localStorage.setItem("burnrate:focus-tool", JSON.stringify({ key: clean, ts: Date.now() }));
            }
          } catch {}
          const targetUrl = clean ? "/?focusTool=" + encodeURIComponent(clean) : "/";
          const handled = await tauriInvoke("show_dashboard", { path: targetUrl });
          if (handled === null) {
            window.open(targetUrl, "burnrate-dashboard");
          }
        }

        let isMini = root.classList.contains("is-mini");
        let comfortable = root.classList.contains("is-comfortable");
        let lastAppliedWidth = 0;
        let lastAppliedHeight = 0;
        const hudWidth = () => isMini ? (comfortable ? 344 : 296) : (comfortable ? 420 : 380);
        function paintDensity() {
          const button = $("hud-density-btn");
          if (!button) return;
          button.textContent = comfortable ? "Comfortable" : "Compact";
          button.setAttribute("aria-pressed", String(comfortable));
          button.setAttribute("aria-label", comfortable ? "Use compact density" : "Use comfortable density");
        }
        $("hud-density-btn")?.addEventListener("click", () => {
          if (morphing) return;
          comfortable = !comfortable;
          root.classList.toggle("is-comfortable", comfortable);
          try { localStorage.setItem("burnrate:hud-density", comfortable ? "comfortable" : "compact"); } catch {}
          paintDensity();
          hideTip();
          syncHudWindowSize(true);
        });
        paintDensity();
        // An opaque window frames the card with the body's 1px border; a
        // glass card carries its own border inside its measured rect.
        const frameExtra = glass ? 0 : 2;
        const maxHudHeight = () => {
          const avail = Number(window.screen?.availHeight) || 0;
          return avail >= 400 ? Math.min(820, Math.floor(avail * 0.85)) : 820;
        };
        const windowHeightFor = cardHeight => Math.max(160, Math.min(maxHudHeight(), Math.ceil(cardHeight) + frameExtra));

        async function setWindowSize(width, height) {
          const handled = await tauriInvoke("set_hud_size", { width, height });
          if (handled !== null) return;
          if (window.opener) {
            try { window.resizeTo(width + 16, height + 38); } catch {}
          }
        }

        function measuredCardHeight() {
          const shell = $("hud-shell");
          return shell.getBoundingClientRect().height || shell.offsetHeight || (isMini ? 200 : 540);
        }

        async function syncHudWindowSize(force = false) {
          // A morph or a tall tip owns the window size until it finishes.
          if (morphing || tip.grown) return;
          const targetWidth = hudWidth();
          const targetHeight = windowHeightFor(measuredCardHeight());
          if (!force && targetWidth === lastAppliedWidth && Math.abs(targetHeight - lastAppliedHeight) <= 2) {
            return;
          }
          lastAppliedWidth = targetWidth;
          lastAppliedHeight = targetHeight;
          await setWindowSize(targetWidth, targetHeight);
          setTimeout(() => {
            if (morphing || tip.grown) return;
            const settled = windowHeightFor(measuredCardHeight());
            if (Math.abs(settled - lastAppliedHeight) > 2) {
              lastAppliedHeight = settled;
              setWindowSize(targetWidth, settled);
            }
          }, 45);
        }

        function growWindowForTip(bottom) {
          const height = Math.min(maxHudHeight(), Math.ceil(bottom));
          if (height <= window.innerHeight) return;
          tip.grown = true;
          clearTimeout(tip.restoreTimer);
          setWindowSize(lastAppliedWidth || hudWidth(), height);
        }

        function paintMiniButton() {
          const btn = $("hud-mini-btn");
          btn.setAttribute("aria-pressed", String(isMini));
          btn.setAttribute("aria-label", isMini ? "Expand HUD" : "Collapse HUD");
          const context = $("hud-context-disclosure");
          if (context) context.open = !isMini;
        }
        paintMiniButton();
        syncHudWindowSize(true);

        // Collapse/expand: the content dissolves, the card glides to its new
        // size with the next layout laid out at its final width (so nothing
        // reflows mid-flight), then the content settles in. A growing card
        // gets its window first; a shrinking one gives it back at the end.
        const MORPH = {fadeOut: 90, resize: 300, fadeIn: 220};
        async function setMini(next) {
          if (morphing || next === isMini) return;
          closeMoreMenu(false);
          try { localStorage.setItem("burnrate:hud-mini", next ? "1" : "0"); } catch {}
          hideTip();
          const shell = $("hud-shell");
          const body = $("hud-body");
          if (matchMedia("(prefers-reduced-motion: reduce)").matches || typeof shell.animate !== "function") {
            isMini = next;
            root.classList.toggle("is-mini", isMini);
            paintMiniButton();
            renderPlans(latestGroups, true);
            syncHudWindowSize(true);
            return;
          }
          morphing = true;
          try {
            const from = shell.getBoundingClientRect();
            shell.classList.add("morphing");
            shell.style.width = from.width + "px";
            shell.style.height = from.height + "px";
            await body.animate(
              [{opacity: 1, transform: "none"}, {opacity: 0, transform: "scale(0.985)"}],
              {duration: MORPH.fadeOut, easing: "cubic-bezier(.4, 0, 1, 1)", fill: "forwards"}
            ).finished;

            isMini = next;
            root.classList.toggle("is-mini", isMini);
            paintMiniButton();
            renderPlans(latestGroups, true);
            const toWidth = hudWidth();
            shell.style.width = toWidth + "px";
            shell.style.height = "auto";
            const toHeight = shell.getBoundingClientRect().height;
            const box = getComputedStyle(shell);
            const inset = ["paddingLeft", "paddingRight", "borderLeftWidth", "borderRightWidth"]
              .reduce((sum, name) => sum + (parseFloat(box[name]) || 0), 0);
            body.style.width = (toWidth - inset) + "px";
            shell.style.width = from.width + "px";
            shell.style.height = from.height + "px";

            if (toWidth > from.width || toHeight > from.height) {
              await setWindowSize(Math.max(toWidth, Math.ceil(from.width)), windowHeightFor(Math.max(toHeight, from.height)));
            }
            const resize = shell.animate(
              [{width: from.width + "px", height: from.height + "px"}, {width: toWidth + "px", height: toHeight + "px"}],
              {duration: MORPH.resize, easing: "cubic-bezier(.22, .8, .24, 1)"}
            );
            body.getAnimations().forEach(animation => animation.cancel());
            const fadeIn = body.animate(
              [{opacity: 0, transform: "translateY(4px)"}, {opacity: 1, transform: "none"}],
              {duration: MORPH.fadeIn, delay: MORPH.resize * 0.2, easing: "cubic-bezier(.2, .7, .3, 1)", fill: "backwards"}
            );
            await Promise.all([resize.finished, fadeIn.finished]);
          } catch {} finally {
            shell.style.width = "";
            shell.style.height = "";
            body.style.width = "";
            shell.classList.remove("morphing");
            body.getAnimations().forEach(animation => animation.cancel());
            morphing = false;
            syncHudWindowSize(true);
          }
        }
        $("hud-mini-btn").addEventListener("click", () => setMini(!isMini));
        $("hud-mini-bar").addEventListener("click", () => {
          if (miniBarDragged) { miniBarDragged = false; return; }
          const key = $("hud-mini-bar").dataset.providerKey || "";
          openToolInDashboard(key);
        });
        $("hud-mini-bar").addEventListener("keydown", event => {
          if (event.key !== "Enter" && event.key !== " ") return;
          event.preventDefault();
          const key = $("hud-mini-bar").dataset.providerKey || "";
          openToolInDashboard(key);
        });

        function applyTheme() {
          const dark = root.dataset.theme === "dark";
          const btn = $("hud-theme-btn");
          if (btn) {
            btn.setAttribute("aria-pressed", String(dark));
            btn.setAttribute("aria-label", dark ? "Switch to light theme" : "Switch to dark theme");
          }
          const lbl = $("hud-theme-label");
          if (lbl) lbl.textContent = dark ? "Light theme" : "Dark theme";
        }
        applyTheme();
        $("hud-theme-btn").addEventListener("click", () => {
          closeMoreMenu(false);
          const theme = root.dataset.theme === "dark" ? "light" : "dark";
          root.dataset.theme = theme;
          try { localStorage.setItem("burnrate:hud-theme", theme); } catch {}
          applyTheme();
        });

        $("hud-poll-btn").addEventListener("click", async event => {
          event.stopPropagation();
          const btn = $("hud-poll-btn");
          if (!btn || btn.disabled) return;
          btn.disabled = true;
          btn.classList.add("spinning");
          try {
            await fetch("/api/spend/quotas/refresh", { method: "POST", cache: "no-store", headers: { "X-BURNRATE-Request": "1" } });
            await refresh(true);
            // A paired spoke answers from its last Hub copy while renewing
            // it; read once more so the Hub's side of the poll shows promptly.
            setTimeout(() => refresh(true), 1500);
          } catch {} finally {
            btn.disabled = false;
            btn.classList.remove("spinning");
          }
        });

        function windowElapsedPct(q) {
          if (!q || !q.resetsAt || q.payg) return null;
          const resetMs = Date.parse(q.resetsAt);
          if (!Number.isFinite(resetMs)) return null;
          const remMs = resetMs - Date.now();
          if (remMs <= 0) return null;
          const text = String((q.limitKey || "") + " " + (q.label || "")).toLowerCase();
          let durMs = null;
          if (text.includes("5h") || text.includes("5-hour") || text.includes("five-hour")) {
            durMs = 5 * 3600 * 1000;
          } else if (text.includes("week") || text.includes("7d")) {
            durMs = 7 * 86400 * 1000;
          } else if (text.includes("day") || text.includes("24h") || text.includes("daily")) {
            durMs = 86400 * 1000;
          } else if (text.includes("month")) {
            // A monthly window runs from the same day of the previous month.
            const start = new Date(resetMs);
            start.setUTCMonth(start.getUTCMonth() - 1);
            durMs = resetMs - start.getTime();
          }
          if (!durMs || remMs > durMs) return null;
          return Math.max(0, Math.min(100, 100 - (remMs / durMs) * 100));
        }

        function relTime(iso) {
          if (!iso) return "";
          const ms = Date.now() - new Date(iso).getTime();
          if (!Number.isFinite(ms)) return "";
          if (ms < 60000) return "just now";
          if (ms < 3600000) return Math.round(ms / 60000) + "m ago";
          if (ms < 86400000) return Math.round(ms / 3600000) + "h ago";
          return Math.round(ms / 86400000) + "d ago";
        }

        function renderMachines(machines) {
          const host = $("hud-machines");
          const list = Array.isArray(machines) ? machines : [];
          const selectedName = selectedMachine === "all" ? "All computers"
            : list.find(item => item.id === selectedMachine)?.name || (selectedMachine === "local" ? "This computer" : selectedMachine);
          if ($("hud-machine-label")) $("hud-machine-label").textContent = selectedName;
          $("hud-computers-foot").textContent =
            list.length === 1 ? "1 computer" : list.length + " computers";
          if (list.length < 2) {
            host.replaceChildren();
            return;
          }
          const items = [{id: "all", name: "All", isLocal: true}, ...list];
          host.replaceChildren(...items.map(item => {
            const btn = document.createElement("button");
            btn.type = "button";
            btn.className = "hud-machine-chip" + (item.id === selectedMachine ? " active" : "");
            btn.setAttribute("role", "radio");
            btn.setAttribute("aria-checked", String(item.id === selectedMachine));
            const dot = document.createElement("i");
            const seenMs = item.lastSeenAt ? Date.parse(item.lastSeenAt) : NaN;
            const isFresh = Boolean(item.isLocal || item.id === "local" || (Number.isFinite(seenMs) && (Date.now() - seenMs) < 120000));
            if (isFresh) dot.className = "live";
            const label = document.createElement("span");
            label.textContent = item.name || item.id;
            btn.append(dot, label);
            labelTip(btn, () => {
              if (item.id === "all") return "Every computer combined";
              if (item.isLocal || item.id === "local") return "This computer";
              const seen = relTime(item.lastSeenAt);
              return "Paired computer" + (seen ? " · seen " + seen : "");
            });
            btn.addEventListener("click", () => {
              if (selectedMachine === item.id) return;
              selectedMachine = item.id;
              machineGeneration++;
              clearMachineView();
              renderMachines(list);
              refresh(true);
              refreshUsage(true);
              refreshMachines();
            });
            return btn;
          }));
        }

        function windowName(key, label) {
          const text = String(label || "").toLowerCase();
          let scope = "";
          if (key === "antigravity") {
            if (text.includes("gemini")) scope = "Gemini";
            else if (text.includes("claude") || text.includes("gpt")) scope = "Other";
          }
          let win = label || "Plan";
          if (text.includes("5-hour") || text.includes("five-hour") || text.includes("5h")) win = "5h";
          else if (text.includes("month")) win = "Month";
          else if (text.includes("week")) win = "Week";
          else if (text.includes("other model")) win = "Other";
          else if (text.includes("model")) win = "Models";
          else if (text.includes("fund") || text.includes("balance")) win = "Balance";
          return [scope, win].filter(Boolean).join(" ");
        }
        function windowShort(key, label) {
          const long = windowName(key, label).toLowerCase();
          const scope = long.startsWith("gemini ") ? "G·" : long.startsWith("other ") && long !== "other" ? "O·" : "";
          const win = long.replace(/^(gemini|other) /, "");
          const short = {"5h": "5H", "week": "WK", "wk": "WK", "month": "MO", "balance": "BAL", "models": "MOD", "other": "OTH"}[win];
          return scope + (short || win.slice(0, 3).toUpperCase());
        }

        function resetIn(iso) {
          if (!iso) return "";
          const ms = new Date(iso).getTime() - Date.now();
          if (!Number.isFinite(ms)) return "";
          if (ms <= 0) return "now";
          const hours = ms / 3600000;
          if (hours < 1) return Math.max(1, Math.round(ms / 60000)) + "m";
          if (hours < 48) return Math.round(hours) + "h";
          return Math.round(hours / 24) + "d";
        }

        // Days a balance lasts at the recent spend, short enough for the reset column.
        const runwayDays = q => q.payg && q.runway && q.runway.days != null && Number.isFinite(Number(q.runway.days))
          ? Number(q.runway.days) : null;
        function runwayShort(days) {
          if (days < 1) return Math.max(1, Math.round(days * 24)) + "h";
          if (days < 100) return Math.round(days) + "d";
          return days < 365 ? Math.round(days / 30) + "mo" : "1y+";
        }
        const runwayState = days => days < 3 ? "dry" : days < 7 ? "tight" : "safe";

        function resetWhen(iso) {
          const date = new Date(iso || "");
          if (Number.isNaN(date.getTime())) return "";
          return date.toLocaleString([], {month: "short", day: "numeric", hour: "numeric", minute: "2-digit"});
        }

        // Feed health: the newest instant each poll delivered data, plus the
        // age the server reports for the data itself. A wedged request, a dead
        // HUD poller and a stalled collector are all visible; none of them can
        // silently leave a number on screen pretending to be current.
        const FEED_LATE_MS = 30000;
        const USAGE_LATE_MS = 45000;
        const DATA_STALE_SECONDS = 600;
        let feedStartedAt = Date.now();
        const feedHealth = {live: 0, usage: 0, machines: 0, freshness: 0, dataAt: null};
        const lastGood = () => feedHealth.live;
        // The payload's age is measured when it was generated; keep the
        // newest data instant across the live and usage reads so neither can
        // rewind the indicator.
        function noteDataAge(data) {
          if (data.lastIngestAgeSeconds == null || data.lastIngestAgeSeconds === "") return;
          const age = Number(data.lastIngestAgeSeconds);
          if (!Number.isFinite(age)) return;
          const generated = Date.parse(data.generatedAt || "") || Date.now();
          const at = generated - age * 1000;
          if (feedHealth.dataAt == null || at > feedHealth.dataAt) feedHealth.dataAt = at;
        }
        const dataAgeSeconds = () => (
          feedHealth.dataAt == null ? null : Math.max(0, (Date.now() - feedHealth.dataAt) / 1000)
        );
        const feedLate = () => (
          (feedHealth.live > 0 && Date.now() - feedHealth.live > FEED_LATE_MS)
          || (Date.now() - (feedHealth.usage || feedStartedAt) > USAGE_LATE_MS)
          || (dataAgeSeconds() != null && dataAgeSeconds() > DATA_STALE_SECONDS)
        );
        function paintUpdated() {
          const foot = $("hud-updated-foot");
          const compact = $("hud-updated-compact");
          if (!feedHealth.live) { foot.textContent = "Updating…"; if (compact) compact.textContent = foot.textContent; foot.classList.remove("stale"); return; }
          const ageS = dataAgeSeconds();
          if (ageS != null) {
            foot.textContent = ageS < 2 ? "Updated now" : "Updated " + ago(Math.round(ageS)) + " ago";
          } else {
            foot.textContent = "Waiting for collection";
          }
          foot.classList.toggle("stale", feedLate());
          if (compact) {
            compact.textContent = foot.textContent;
            compact.classList.toggle("stale", feedLate());
          }
          if (typeof document !== "undefined") document.querySelectorAll(".win-freshness[data-polled-at]").forEach(node => {
            node.textContent = "Held · " + (relTime(node.dataset.polledAt) || "last known reading");
          });
          if (feedLate()) {
            const pill = $("hud-status-pill");
            pill.dataset.state = "stale";
            $("hud-status-text").textContent = "STALE";
            const miniAlert = $("hud-mini-alert");
            miniAlert.hidden = false;
            miniAlert.dataset.state = "stale";
            miniAlert.textContent = "Usage needs attention";
            if (typeof paintAttention === "function") paintAttention();
          }
        }

        // Stat tips read the latest usage response when shown.
        let usageView = {nav: {}, totals: {}, importedAt: null};
        const importedFoot = () => {
          const imported = relTime(usageView.importedAt);
          return imported ? "Usage imported " + imported : "";
        };
        function usageGapMessage(nav) {
          if (nav.todayComplete !== false) return "";
          const tokens = Number(nav.todayUnpricedTokens) || 0;
          if (nav.todayUsd == null && nav.todayTokens === 0 && !tokens) return "No recorded usage is available to value yet.";
          const models = (nav.todayUnpricedModels || []).map(model => String(model).replace(/^[a-z-]+:/, ""));
          return (nav.todayUsd == null ? "Value unavailable. " : "At least this much. ") +
            (tokens > 0 ? tok(tokens) + " tokens" : "Some usage") +
            " cannot be valued because rates or token details are missing" +
            (models.length ? " (" + models.join(", ") + ")" : "") + ".";
        }
        setTip($("hud-stat-today"), () => {
          const nav = usageView.nav;
          return T.head("API-rate value", "today") +
            T.hero(usd2(nav.todayUsd), "", "since local midnight") +
            T.line("What today's usage would cost at published API rates. Not a bill.", "dim") +
            (usageGapMessage(nav) ? T.line(usageGapMessage(nav), "tight") : "") +
            T.foot(importedFoot());
        });
        setTip($("hud-stat-burn"), () => {
          const burn = usageView.nav.burnRatePerDay;
          return T.head("Burn rate", "7-day average") +
            T.hero(burn == null ? "—" : usd(burn), "", "per day") +
            T.line("Average API-rate value over the last 7 full days. Today is not counted.", "dim") +
            T.foot(importedFoot());
        });
        setTip($("hud-stat-cache"), () => {
          const cache = usageView.totals.cacheReusePct;
          return T.head("Cache hit", "last 24 hours") +
            T.hero(cache == null ? "—" : Math.round(Number(cache)) + "%", "", "of prompt tokens") +
            T.line("Prompt tokens served from cache instead of processed fresh.", "dim") +
            T.foot(importedFoot());
        });
        setTip($("hud-stat-tokens"), () => {
          const nav = usageView.nav;
          return T.head("Tokens", "today") +
            T.hero(tok1(nav.todayTokens), "", "since local midnight") +
            T.kv([nav.tokensAvgPerDay == null ? null : ["7-day average", tok1(nav.tokensAvgPerDay) + " / day"]]) +
            T.foot(importedFoot());
        });

        const shortNames = {
          codex: "Codex",
          "claude-code": "Claude",
          antigravity: "Antigravity",
          grok: "Grok",
          opencode: "Z.AI",
          "opencode-go": "OpenCode Go",
          muse: "Muse",
          openrouter: "OpenRouter",
          deepseek: "DeepSeek"
        };
        // Provider marks come from the shared sprite served at /logos.svg.
        const PLAN_LOGOS = {
          codex: "codex", "claude-code": "claude", grok: "grok", opencode: "zai", "opencode-go": "opencode",
          antigravity: "antigravity", muse: "meta", openrouter: "openrouter", traycer: "traycer"
        };
        const MODEL_LOGOS = [
          [/claude|opus|sonnet|haiku/i, "claude"], [/gpt|codex/i, "codex"], [/grok/i, "grok"],
          [/glm|z\.ai/i, "zai"], [/muse/i, "meta"], [/gemini/i, "gemini"]
        ];
        const iconFor = id => {
          const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
          svg.setAttribute("viewBox", "0 0 24 24");
          svg.setAttribute("aria-hidden", "true");
          const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
          use.setAttribute("href", "/logos.svg#" + id);
          svg.append(use);
          return svg;
        };

        function planGroups(data) {
          const groups = [];
          for (const provider of (data.capacity || [])) {
            const key = provider.providerKey || "";
            const name = shortNames[key] || provider.shortName || provider.providerName || key || "Plan";
            const sourceRows = Array.isArray(provider.rows) && provider.rows.length ? provider.rows : [provider];
            const rows = sourceRows.map(row => {
              const value = row.pct == null || row.pct === "" ? NaN : Number(row.pct);
              return {
                limitKey: row.limitKey || "",
                label: row.label || row.limitKey || provider.plan || "",
                title: [provider.providerName || name, row.label || provider.plan || ""].filter(Boolean).join(" · "),
                pct: Number.isFinite(value) ? value : null,
                payg: Boolean(row.isPayg || provider.isPayg),
                used: row.used == null || row.used === "" ? null : Number(row.used),
                allowance: row.allowance == null || row.allowance === "" ? null : Number(row.allowance),
                unit: row.unit || "",
                source: row.source || "",
                resetsAt: row.resetsAt || "",
                polledAt: row.polledAt || row.observedAt || provider.polledAt || "",
                forecast: row.forecast && typeof row.forecast === "object" ? row.forecast : {},
                runway: row.runway && typeof row.runway === "object" ? row.runway : null,
                held: Boolean(row.held)
              };
            });
            const polledAt = provider.polledAt || (rows.find(r => r.polledAt)?.polledAt) || "";
            // An extra Claude account lane opens Claude Code's tool view.
            const toolKey = provider.toolKey || key;
            if (rows.length) groups.push({ key, toolKey, name, accountLabel: provider.accountLabel || "", plan: provider.plan || "", rows, providerName: provider.providerName || name, polledAt });
          }
          return groups;
        }
        // The last real reading of every window, per computer filter. A poll
        // that comes back without a value (a slow provider, a Hub hiccup, a
        // lane between samples) keeps the number on screen until the window
        // resets, and a card missing from one response stays for a while.
        const heldReadings = new Map();
        const HELD_CARD_MS = 15 * 60 * 1000;
        const windowOpen = q => {
          const reset = Date.parse(q.resetsAt || "");
          return !Number.isFinite(reset) || reset > Date.now();
        };
        function holdReadings(groups) {
          const now = Date.now();
          const scope = selectedMachine + "|";
          const seen = new Set();
          for (const group of groups) {
            const key = scope + group.key;
            seen.add(key);
            const prior = heldReadings.get(key);
            group.rows = group.rows.map(q => {
              if (q.pct != null || q.payg) return q;
              const last = prior && prior.group.rows.find(row => row.limitKey === q.limitKey);
              return last && last.pct != null && windowOpen(last) ? {...last, held: true} : q;
            });
            heldReadings.set(key, {group, seenAt: now});
          }
          for (const [key, entry] of heldReadings) {
            if (seen.has(key) || !key.startsWith(scope)) continue;
            if (now - entry.seenAt > HELD_CARD_MS) { heldReadings.delete(key); continue; }
            const rows = entry.group.rows.filter(q => q.pct != null && windowOpen(q)).map(q => ({...q, held: true}));
            if (rows.length) groups.push({...entry.group, rows});
          }
          return groups;
        }
        const shownPeak = group => {
          const finite = group.rows.map(item => item.pct).filter(value => value != null);
          if (!finite.length) return group.rows.some(item => item.payg) ? -2 : -1;
          return Math.max(...finite);
        };

        // Tokens burned inside each plan window, from the usage read and
        // scoped to the selected computer like every other usage figure.
        let windowTokens = {};
        const burnFor = (group, q) => {
          const lane = (windowTokens[group.key] || {})[q.limitKey];
          return lane && Number.isFinite(Number(lane.tokens)) ? lane : null;
        };

        function windowTitle(key, label) {
          const name = windowName(key, label);
          const scoped = /^(Gemini|Other) (.+)$/.exec(name);
          const win = scoped ? scoped[2] : name;
          const text = {"5h": "5-hour window", "Week": "Weekly window", "Wk": "Weekly window", "Month": "Monthly window"}[win] || win;
          return scoped ? scoped[1] + " · " + text.toLowerCase() : text;
        }

        function modelList(today) {
          const models = Array.isArray(today.models) ? today.models : [];
          if (!models.length) return T.line("Nothing yet since midnight.", "dim");
          const total = Number(today.tokens) || models.reduce((sum, m) => sum + Number(m.tokens || 0), 0) || 1;
          const items = models.slice(0, 4).map(m =>
            "<li><span>" + esc(m.name) + "</span><b>" + esc(tok1(m.tokens)) + '</b><i style="--share:' +
            Math.min(100, Number(m.tokens || 0) / total * 100).toFixed(1) + '%"></i></li>');
          const rest = models.slice(4);
          if (rest.length) {
            items.push('<li class="more"><span>' + esc(rest.length === 1 ? "1 more model" : rest.length + " more models") + "</span><b>" +
              esc(tok1(rest.reduce((sum, m) => sum + Number(m.tokens || 0), 0))) + "</b></li>");
          }
          return '<ul class="tt-models">' + items.join("") + "</ul>";
        }

        function windowTip(group, q) {
          const f = q.forecast || {};
          const fstate = f.state || "";
          let html = T.head(group.providerName || group.name, windowTitle(group.key, q.label));
          if (q.payg) {
            const balance = q.unit === "usd" && q.used != null ? usd2(q.used) : (q.used != null ? qtyText(q.used, q.unit) : null);
            html += T.hero(balance || "—", "", balance ? "left · pay as you go" : "pay as you go");
          } else if (q.pct != null) {
            const until = resetIn(q.resetsAt);
            html += T.hero(pctText(q.pct), until ? (until === "now" ? "resets now" : "resets in " + until) : "", resetWhen(q.resetsAt));
          } else {
            html += T.hero("—", "", "no reading");
          }

          const rows = [];
          const runway = q.payg ? q.runway : null;
          const days = runwayDays(q);
          if (days != null) {
            const spend = usd2(runway.dailyUsd) + "/day over the last " + Math.max(1, Math.round(Number(runway.basisDays) || 0)) + " days";
            rows.push(["Runway", String(runway.line || "").split(" at ")[0] || runwayShort(days), runwayState(days), spend]);
          } else if (runway && runway.line) {
            rows.push(["Runway", runway.state === "idle" ? "no recent spend" : "learning", runway.state === "idle" ? "safe" : "", runway.line]);
          }
          if (!q.payg && q.allowance != null && q.allowance > 0 && q.used != null) {
            rows.push(["Used", qtyText(q.used, q.unit) + " of " + qtyText(q.allowance, q.unit)]);
          }
          const lane = q.payg ? null : burnFor(group, q);
          if (lane) {
            const tokens = Number(lane.tokens);
            rows.push(["Burned", tokens ? tok1(tokens) + " tokens" : "none recorded", "",
              tokens ? tok1(lane.input) + " in · " + tok1(lane.output) + " out · " + tok1(lane.cached) + " cached" : ""]);
          }
          // One pace verdict: a run-dry forecast from the recent burn wins
          // over the whole-window average, which it would otherwise contradict.
          const elapsed = windowElapsedPct(q);
          if (fstate === "tight" || fstate === "dry") {
            const [when, before] = String(f.line || "").replace(/^pace\s*→\s*/, "").replace(/\)$/, "").split(" (");
            const lately = f.burnPctPerHour != null ? "+" + (Math.round(f.burnPctPerHour * 10) / 10).toFixed(1) + "%/h lately" : "";
            const measured = f.basis === "usage" && f.tokensPerHour ? " · " + tok1(f.tokensPerHour) + " tok/h measured" : "";
            rows.push(["Pace", when || "running dry", fstate, [before, lately + measured].filter(Boolean).join(" · ")]);
          } else if (elapsed != null && q.pct != null && !q.payg) {
            const delta = Math.round(q.pct - elapsed);
            rows.push(["Pace",
              delta > 2 ? delta + "% ahead of even" : delta < -2 ? Math.abs(delta) + "% under even" : "on even pace",
              delta >= 15 ? "dry" : delta >= 5 ? "tight" : "safe",
              Math.round(elapsed) + "% of the window gone"]);
          } else if (fstate === "stale" && q.pct != null) {
            rows.push(["Pace", "unknown", "", "the newest sample is stale"]);
          }
          html += T.kv(rows);
          const detail = String(q.label || "").split(" · ").slice(1).join(" · ");
          if (q.pct == null && !q.payg && detail) html += T.line(detail, "dim");

          if (lane && lane.today) html += T.sep + T.section("Today by model", tok1(lane.today.tokens)) + modelList(lane.today);

          const age = relTime(q.polledAt);
          if (q.held) html += T.line("Showing the reading from " + (age || "earlier") + "; the newest poll came back empty.", "tight");
          else html += T.foot(age ? "Polled " + age : "");
          return html;
        }

        function windowRow(group, q, isPeak) {
          const cell = document.createElement("div");
          cell.className = "win" + (isPeak ? " is-peak" : "") + (q.held ? " held" : "");
          const fstate = (q.forecast || {}).state || "";
          const label = document.createElement("span");
          label.className = "win-label";
          const longLabel = document.createElement("span");
          longLabel.className = "long";
          longLabel.textContent = windowName(group.key, q.label);
          const shortLabel = document.createElement("span");
          shortLabel.className = "short";
          shortLabel.textContent = windowShort(group.key, q.label);
          label.append(longLabel, shortLabel);
          const meter = document.createElement("div");
          meter.className = "meter";
          meter.setAttribute("aria-hidden", "true");
          const fill = document.createElement("i");
          fill.className = "fill";
          meter.append(fill);
          if (q.pct != null) {
            const p = Math.max(0, Math.min(100, q.pct));
            meter.style.setProperty("--fill", p.toFixed(1) + "%");
            if (fstate === "dry" || p >= 90) cell.classList.add("crit");
            else if (fstate === "tight" || p >= 75) cell.classList.add("warn");
          }
          const elapsed = windowElapsedPct(q);
          if (elapsed != null) {
            const tick = document.createElement("i");
            tick.className = "win-pace-tick";
            tick.style.left = elapsed.toFixed(2) + "%";
            meter.append(tick);
          }
          const value = document.createElement("b");
          value.className = "win-value";
          const number = document.createElement("span");
          number.className = "win-number";
          if (q.pct == null) {
            const full = document.createElement("span"), short = document.createElement("span");
            full.className = "long"; full.textContent = "No reading";
            short.className = "short"; short.textContent = "—";
            number.append(full, short);
          } else number.textContent = pctText(q.pct);
          const valueUnit = document.createElement("span");
          valueUnit.className = "win-unit";
          valueUnit.textContent = q.pct == null ? "" : " used";
          value.append(number, valueUnit);
          const eta = document.createElement("i");
          eta.className = "win-reset";
          const reset = resetIn(q.resetsAt);
          const resetPrefix = document.createElement("span");
          resetPrefix.className = "win-reset-prefix";
          resetPrefix.textContent = reset ? (reset === "now" ? "Resets " : "Resets in ") : "Reset ";
          const resetValue = document.createElement("span");
          resetValue.className = "win-reset-value";
          if (reset) resetValue.textContent = reset;
          else {
            const full = document.createElement("span"), short = document.createElement("span");
            full.className = "long"; full.textContent = "unavailable";
            short.className = "short"; short.textContent = "—";
            resetValue.append(full, short);
          }
          eta.append(resetPrefix, resetValue);
          cell.append(label, meter, value, eta);
          if (q.held) {
            const age = document.createElement("span");
            age.className = "win-freshness";
            age.dataset.polledAt = q.polledAt || "";
            age.textContent = "Held · " + (relTime(q.polledAt) || "last known reading");
            cell.append(age);
          }
          setTip(cell, () => windowTip(group, q));
          return cell;
        }

        const planExpansion = new Map();
        let latestGroups = [];
        let pinnedProviders = [];
        try {
          const saved = JSON.parse(localStorage.getItem("burnrate:hud-pins") || "[]");
          if (Array.isArray(saved)) pinnedProviders = [...new Set(saved.filter(key => typeof key === "string"))];
        } catch {}
        function providerOrder(a, b) {
          const ai = pinnedProviders.indexOf(a.key), bi = pinnedProviders.indexOf(b.key);
          if (ai >= 0 || bi >= 0) return ai < 0 ? 1 : bi < 0 ? -1 : ai - bi;
          return a.name.localeCompare(b.name) || a.key.localeCompare(b.key);
        }
        function restorePlanFocus(key) {
          if (!key) return;
          const next = [...document.querySelectorAll("[data-plan-focus]")].find(button => button.dataset.planFocus === key);
          next?.focus({preventScroll: true});
        }
              let lastBalancesSig = "", lastPlansSig = "", lastAgentsSig = "";
        function renderBalances(groups, force = false) {
          const host = $("hud-balances"), section = $("hud-balances-section");
          if (!host || !section) return;
          const balances = groups.flatMap(group => group.rows.filter(q => q.payg).map(q => ({group, q})));
          section.hidden = !balances.length;
          const sig = JSON.stringify(balances.map(({group, q}) => [
            group.key, group.name, group.providerName, q.label, q.unit, q.used, q.runway, q.polledAt, q.held
          ]));
          if (!force && sig === lastBalancesSig) return;
          lastBalancesSig = sig;
          host.replaceChildren(...balances.map(({group, q}) => {
            const row = document.createElement("article");
            row.className = "balance-row";
            row.dataset.providerKey = group.key;
            const name = document.createElement("strong");
            name.className = "balance-name";
            name.textContent = group.name;
            const value = document.createElement("b");
            value.className = "balance-value";
            const known = q.unit === "usd" && q.used != null;
            value.textContent = known ? usd2(q.used) : "—";
            const unit = document.createElement("span");
            unit.className = "balance-unit";
            unit.textContent = known ? "remaining" : "Balance unavailable";
            const runway = document.createElement("span");
            runway.className = "balance-runway";
            const days = runwayDays(q);
            runway.textContent = days != null ? "Estimated runway · " + runwayShort(days)
              : q.runway?.state === "idle" ? "No recent spend" : "Runway unavailable";
            if (days != null && days < 3) row.classList.add("crit");
            else if (days != null && days < 7) row.classList.add("warn");
            row.append(name, value, runway, unit);
            setTip(row, () => windowTip(group, q));
            return row;
          }));
        }
        function renderUrgent(groups) {
          const host = $("hud-urgent");
          if (!host) return;
          const windows = groups.flatMap(group => group.rows.filter(q => !q.payg && q.pct != null).map(q => ({group, q})));
          windows.sort((a, b) => {
            const risk = q => (q.forecast?.state === "dry" ? 200 : q.forecast?.state === "tight" ? 100 : 0) + q.pct;
            return risk(b.q) - risk(a.q);
          });
          const lead = windows[0];
          host.hidden = !lead;
          if (!lead) { host.textContent = ""; return; }
          host.dataset.state = lead.q.pct >= 90 || lead.q.forecast?.state === "dry" ? "critical"
            : lead.q.pct >= 75 || lead.q.forecast?.state === "tight" ? "warning" : "quiet";
          host.textContent = (lead.q.held ? "Closest known limit · " : "Closest limit · ") + lead.group.name + " · " + pctText(lead.q.pct) + " used" +
            (lead.q.held ? " · held " + (relTime(lead.q.polledAt) || "reading") : "");
        }
        function renderPlans(groups, force = false) {
          latestGroups = groups;
          const plans = groups.map(group => ({...group, rows: group.rows.filter(q => !q.payg)}))
            .filter(group => group.rows.length).sort(providerOrder);
          const badge = $("hud-quota-badge");
          badge.textContent = plans.length ? String(plans.length) : "—";
          labelTip(badge, plans.length ? plans.length + " plans · pinned first, then alphabetical" : "No plans yet");
          const sig = JSON.stringify([
            selectedMachine, isMini, dashboardOn, pinnedProviders,
            plans.map(group => {
              const expansionKey = selectedMachine + "|" + group.key;
              const expanded = planExpansion.has(expansionKey) ? planExpansion.get(expansionKey) : !isMini;
              return [
                group.key, group.toolKey, group.name, group.providerName, group.accountLabel, group.plan, group.polledAt, expanded,
                group.rows.map(q => [
                  q.limitKey, q.label, q.pct, q.used, q.allowance, q.unit, q.resetsAt, q.polledAt, q.held, q.forecast,
                  resetIn(q.resetsAt), q.held ? relTime(q.polledAt) : "", Math.round((windowElapsedPct(q) ?? -1) * 10)
                ])
              ];
            })
          ]);
          if (force || sig !== lastPlansSig) {
            lastPlansSig = sig;
            const focused = document.activeElement?.dataset.planFocus;
            const quotasRoot = $("hud-quotas");
            if (!plans.length) quotasRoot.innerHTML = '<p class="hud-empty">No subscription usage yet.</p>';
            else quotasRoot.replaceChildren(...plans.map(group => {
              const card = document.createElement("article");
              card.className = "plan";
              card.setAttribute("aria-label", group.name);
              card.dataset.providerKey = group.key;
              const expansionKey = selectedMachine + "|" + group.key;
              const expanded = planExpansion.has(expansionKey) ? planExpansion.get(expansionKey) : !isMini;
              card.classList.toggle("open", expanded);
              const head = document.createElement("header");
              head.className = "plan-head";
              const toggle = document.createElement("button");
              toggle.type = "button";
              toggle.className = "plan-toggle";
              toggle.dataset.planFocus = "toggle:" + group.key;
              const extraId = "hud-extra-" + encodeURIComponent(group.key);
              toggle.setAttribute("aria-controls", extraId);
              toggle.setAttribute("aria-expanded", String(expanded));
              toggle.setAttribute("aria-label", group.name + ", " + group.rows.length + (group.rows.length === 1 ? " window" : " windows"));
              const mark = document.createElement("span");
              mark.className = "mark";
              const extraLogo = String(group.key).startsWith("claude") ? "claude" : String(group.key).startsWith("opencode-go-") ? "opencode" : "custom";
              mark.append(iconFor(PLAN_LOGOS[group.key] || extraLogo));
              const name = document.createElement("b");
              name.textContent = group.name;
              const count = document.createElement("span");
              count.className = "plan-window-count";
              count.textContent = group.rows.length + (group.rows.length === 1 ? " window" : " windows");
              const chevron = document.createElement("span");
              chevron.className = "plan-chevron";
              chevron.setAttribute("aria-hidden", "true");
              chevron.textContent = "›";
              toggle.append(mark, name, count, chevron);
              const pin = document.createElement("button");
              pin.type = "button";
              pin.className = "plan-pin";
              pin.dataset.planFocus = "pin:" + group.key;
              const isPinned = pinnedProviders.includes(group.key);
              pin.setAttribute("aria-pressed", String(isPinned));
              pin.setAttribute("aria-label", (isPinned ? "Unpin " : "Pin ") + group.name);
              pin.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 16v5M9 4h6l-1 5 3 3v3H7v-3l3-3z"/></svg>';
              labelTip(pin, (isPinned ? "Unpin " : "Pin ") + group.name);
              pin.addEventListener("click", () => {
                pinnedProviders = isPinned ? pinnedProviders.filter(key => key !== group.key) : [...pinnedProviders, group.key];
                try { localStorage.setItem("burnrate:hud-pins", JSON.stringify(pinnedProviders)); } catch {}
                renderPlans(latestGroups, true);
                syncHudWindowSize();
              });
              head.append(toggle, pin);
              if (dashboardOn) {
                const detail = document.createElement("button");
                detail.type = "button";
                detail.className = "plan-detail";
                detail.dataset.planFocus = "detail:" + group.key;
                detail.setAttribute("aria-label", "Open " + group.name + " details");
                detail.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M14 4h6v6M20 4l-9 9M18 14v5H5V6h5"/></svg>';
                labelTip(detail, "Open " + group.name + " details");
                detail.addEventListener("click", () => openToolInDashboard(group.toolKey || group.key));
                head.append(detail);
              }
              const windows = document.createElement("div");
              windows.className = "windows";
              const peak = group.rows.reduce((best, row) => row.pct != null && (best == null || row.pct > best.pct) ? row : best, null) || group.rows[0];
              windows.append(windowRow(group, peak, true));
              const extra = document.createElement("div");
              extra.className = "plan-extra";
              extra.id = extraId;
              extra.hidden = !expanded;
              for (const q of group.rows) if (q !== peak) extra.append(windowRow(group, q, false));
              toggle.disabled = group.rows.length < 2;
              if (toggle.disabled) chevron.hidden = true;
              toggle.addEventListener("click", () => {
                const next = extra.hidden;
                planExpansion.set(expansionKey, next);
                extra.hidden = !next;
                toggle.setAttribute("aria-expanded", String(next));
                card.classList.toggle("open", next);
                hideTip();
                syncHudWindowSize();
              });
              windows.append(extra);
              card.append(head, windows);
              return card;
            }));
            restorePlanFocus(focused);
          }
          renderBalances(groups.slice().sort((a, b) => a.name.localeCompare(b.name)), force);
          renderUrgent(plans);
        }

        function renderAgents(data) {
          const liveAgents = (data.activity || []).filter(item => String(item.state || "").toUpperCase() === "LIVE");
          const miniLive = $("hud-mini-live");
          miniLive.hidden = !liveAgents.length;
          miniLive.textContent = liveAgents.length ? "● " + liveAgents.length + " live" : "";
          const liveSection = $("hud-live");
          const agentsRoot = $("hud-agents");
          liveSection.hidden = !liveAgents.length;
          const sig = JSON.stringify([dashboardOn, liveAgents.length, liveAgents.slice(0, 3).map(a => [a.name, a.modelKey, a.machineName])]);
          if (sig === lastAgentsSig) return;
          lastAgentsSig = sig;
          agentsRoot.replaceChildren(...liveAgents.slice(0, 3).map(agent => {
            const row = document.createElement("div");
            row.className = "hud-agent";
            const mainWrap = document.createElement("div");
            mainWrap.className = "hud-agent-main";
            const pulse = document.createElement("span");
            pulse.className = "hud-agent-mark";
            const modelId = MODEL_LOGOS.find(([pattern]) => pattern.test(String(agent.modelKey || "")))?.[1] || "custom";
            const badge = document.createElement("i");
            badge.className = "hud-pulse";
            pulse.append(iconFor(modelId), badge);
            const textWrap = document.createElement("div");
            textWrap.className = "hud-agent-text";
            const rawName = String(agent.name || "Active session").replace(/\s+/g, " ").trim() || "Active session";
            const shortName = rawName.length > 54 ? rawName.slice(0, 53).trimEnd() + "…" : rawName;
            const strong = document.createElement("strong");
            strong.textContent = shortName;
            const small = document.createElement("small");
            small.textContent = agent.modelKey || "live";
            textWrap.append(strong, small);
            mainWrap.append(pulse, textWrap);
            const tag = document.createElement("span");
            tag.className = "hud-machine-tag";
            tag.textContent = agent.machineName || "LIVE";
            row.append(mainWrap, tag);
            const modelLower = String(agent.modelKey || "").toLowerCase();
            const inferredTool = modelLower.includes("claude") ? "claude-code" : modelLower.includes("gpt") || modelLower.includes("codex") ? "codex" : modelLower.includes("grok") ? "grok" : modelLower.includes("muse") ? "muse" : "";
            const whereLabel = [agent.modelKey || "live", agent.machineName || ""].filter(Boolean).join(" · ");
            setTip(row, () => T.head(shortName) + T.line(whereLabel, "dim") + (dashboardOn ? T.foot("Click to open it in the dashboard") : ""));
            if (dashboardOn) row.addEventListener("click", () => openToolInDashboard(inferredTool));
            return row;
          }));
        }

        let heldStats = {day: "", byMachine: {}};
        function renderUsage(data) {
          const nav = data.navigation || {};
          const totals = data.totals || {};
          $("hud-date").textContent = "Today · " + new Date().toLocaleDateString("en-US", {
            weekday: "short", month: "short", day: "numeric"
          });

          // A failed transport never calls renderUsage, so its last figures
          // remain visible with feed-age warnings. A successful response can
          // explicitly report null: that means unavailable, not a held value.
          // Omitted fields from older hubs can retain this computer's value.
          const day = new Date().toDateString();
          if (heldStats.day !== day) heldStats = {day, byMachine: {}};
          const slot = heldStats.byMachine[selectedMachine] || (heldStats.byMachine[selectedMachine] = {});
          const keep = (name, object, key) => Object.prototype.hasOwnProperty.call(object, key)
            ? (slot[name] = object[key] == null || object[key] === "" ? null : object[key])
            : slot[name];
          const today = keep("today", nav, "todayUsd");
          const burn = keep("burn", nav, "burnRatePerDay");
          const cache = keep("cache", totals, "cacheReusePct");
          setStat("hud-burn-rate", burn != null ? usd(burn) : "—");
          // A day with usage no card can price is a floor: mark the figure.
          const floor = nav.todayComplete === false && today != null ? "≥" : "";
          setStat("hud-today", floor + usd2(today));
          setStat("hud-mini-today", floor + usd2(today));
          setStat("hud-cache", cache == null ? "—" : Math.round(Number(cache)) + "%");
          setStat("hud-tokens", tok(nav.todayTokens ?? 0));
          usageView = {nav: {...nav, todayUsd: today, burnRatePerDay: burn}, totals: {cacheReusePct: cache}, importedAt: data.historyUpdatedAt};
          if (typeof paintCoverage === "function") paintCoverage();
          noteDataAge(data);
          // An older hub sends no window tokens; meter tips then omit them.
          if (data.windowTokens && typeof data.windowTokens === "object") windowTokens = data.windowTokens;
          renderRepos(data.repos);
          refreshTip();
        }

        let noticeTimer = 0;
        function notice(text) {
          const line = $("hud-notice");
          line.textContent = text;
          line.hidden = false;
          clearTimeout(noticeTimer);
          noticeTimer = setTimeout(() => { line.hidden = true; }, 4000);
        }

        // The weekly card: a 1200 x 630 image of the last seven days, copied
        // to the clipboard. Totals and tool names only, never repositories.
        const CARD_GAUGE = ["M19.9 42L13 46M18 35H10M19.9 28L13 24M25 22.9L21 16M32 21V13M39 22.9L43 16",
                            "M44.1 28L51 24M46 35H54M44.1 42L51 46", "M32 35L44.5 22.5"];
        function drawWeekCard(week) {
          const canvas = document.createElement("canvas");
          canvas.width = 1200;
          canvas.height = 630;
          const ctx = canvas.getContext("2d");
          const display = "Bahnschrift, 'DIN Alternate', 'Segoe UI', system-ui, sans-serif";
          const body = "'Segoe UI Variable Text', 'Segoe UI', system-ui, -apple-system, sans-serif";
          const ink = "#f2f0eb", muted = "#9a968f", ember = "#ff6b22", line = "rgba(255,255,255,0.12)";
          ctx.fillStyle = "#0a0a0c";
          ctx.fillRect(0, 0, 1200, 630);
          ctx.fillStyle = "#121215";
          ctx.fillRect(40, 40, 1120, 550);
          ctx.strokeStyle = line;
          ctx.strokeRect(40.5, 40.5, 1119, 549);
          // Gauge mark and wordmark.
          ctx.save();
          ctx.translate(80, 76);
          ctx.scale(0.75, 0.75);
          ctx.lineCap = "round";
          ctx.lineWidth = 4.2;
          ctx.strokeStyle = ink;
          ctx.stroke(new Path2D(CARD_GAUGE[0]));
          ctx.strokeStyle = ember;
          ctx.stroke(new Path2D(CARD_GAUGE[1]));
          ctx.lineWidth = 3;
          ctx.stroke(new Path2D(CARD_GAUGE[2]));
          ctx.fillStyle = ink;
          ctx.beginPath();
          ctx.arc(32, 35, 4.4, 0, Math.PI * 2);
          ctx.fill();
          ctx.restore();
          ctx.fillStyle = ink;
          ctx.font = "600 22px " + display;
          if ("letterSpacing" in ctx) ctx.letterSpacing = "5px";
          ctx.fillText("BURNRATE", 140, 113);
          if ("letterSpacing" in ctx) ctx.letterSpacing = "0px";
          const from = new Date(week.from), to = new Date(week.to);
          const range = from.toLocaleDateString("en-US", {month: "short", day: "numeric"}) + " – " +
            to.toLocaleDateString("en-US", {month: "short", day: "numeric", year: "numeric"});
          ctx.fillStyle = muted;
          ctx.font = "20px " + body;
          ctx.textAlign = "right";
          ctx.fillText("Last 7 days · " + range, 1120, 110);
          ctx.textAlign = "left";
          // Headline: the week's value at API rates.
          ctx.fillStyle = ink;
          ctx.font = "400 120px " + display;
          const valuePrefix = week.complete === false && week.usd != null ? "≥" : "";
          ctx.fillText(valuePrefix + usd(week.usd), 80, 270);
          ctx.fillStyle = muted;
          ctx.font = "26px " + body;
          ctx.fillText("of AI coding at published API rates", 84, 316);
          if (week.planUsd && week.usd != null) {
            ctx.fillStyle = ember;
            ctx.font = "600 26px " + body;
            const times = week.usd / week.planUsd;
            ctx.fillText("on " + usd(week.planUsd) + " of plans" + (times >= 1.5 ? " · " + times.toFixed(1) + "× the plan cost" : ""), 84, 356);
          }
          // Stats row.
          const stats = [["Tokens", tok(week.tokens)], ["Cache hit", week.cacheReusePct == null ? "—" : Math.round(week.cacheReusePct) + "%"],
                         ["Sessions", String(week.sessions || 0)]];
          stats.forEach(([label, value], index) => {
            const x = 84 + index * 190;
            ctx.fillStyle = muted;
            ctx.font = "600 15px " + display;
            if ("letterSpacing" in ctx) ctx.letterSpacing = "2px";
            ctx.fillText(label.toUpperCase(), x, 446);
            if ("letterSpacing" in ctx) ctx.letterSpacing = "0px";
            ctx.fillStyle = ink;
            ctx.font = "400 44px " + display;
            ctx.fillText(value, x, 496);
          });
          // Top tools with share bars.
          const tools = (week.tools || []).slice(0, 4);
          const top = Math.max(...tools.map(tool => tool.usd), 0);
          ctx.fillStyle = muted;
          ctx.font = "600 15px " + display;
          if ("letterSpacing" in ctx) ctx.letterSpacing = "2px";
          ctx.fillText("TOP TOOLS", 760, 196);
          if ("letterSpacing" in ctx) ctx.letterSpacing = "0px";
          tools.forEach((tool, index) => {
            const y = 240 + index * 70;
            ctx.fillStyle = ink;
            ctx.font = "600 22px " + body;
            ctx.fillText(tool.name, 760, y);
            ctx.fillStyle = muted;
            ctx.font = "20px " + body;
            ctx.textAlign = "right";
            ctx.fillText(usd(tool.usd), 1120, y);
            ctx.textAlign = "left";
            ctx.fillStyle = "rgba(255,255,255,0.08)";
            ctx.fillRect(760, y + 14, 360, 6);
            ctx.fillStyle = ember;
            ctx.fillRect(760, y + 14, top ? Math.max(4, 360 * tool.usd / top) : 0, 6);
          });
          // Footer.
          ctx.strokeStyle = line;
          ctx.beginPath();
          ctx.moveTo(80, 540.5);
          ctx.lineTo(1120, 540.5);
          ctx.stroke();
          ctx.fillStyle = muted;
          ctx.font = "18px " + body;
          ctx.fillText("Measured locally with BURNRATE · nothing left my computer", 80, 572);
          ctx.textAlign = "right";
          ctx.fillText("github.com/kamol-nazarov/myBurnRate", 1120, 572);
          ctx.textAlign = "left";
          return canvas;
        }

        async function shareWeek() {
          let week;
          try {
            week = await hudFetch("/api/spend/share/week?machine=" + encodeURIComponent(selectedMachine || "all"));
          } catch {
            notice("This week's card could not be made");
            return;
          }
          const blob = await new Promise(resolve => drawWeekCard(week).toBlob(resolve, "image/png"));
          try {
            await navigator.clipboard.write([new ClipboardItem({"image/png": blob})]);
            notice("This week's card is on the clipboard · paste it anywhere");
          } catch {
            window.open(URL.createObjectURL(blob), "_blank");
            notice("This week's card opened in a new window");
          }
        }

        // Today's value by repository; an older hub sends none and the section stays hidden.
        let lastReposSig = "";
        function renderRepos(repos) {
          const rows = repos && Array.isArray(repos.rows) ? repos.rows.filter(row => Number(row.usd) > 0) : [];
          $("hud-repos").hidden = !rows.length;
          if (!rows.length) {
            if (lastReposSig !== "empty") {
              lastReposSig = "empty";
              $("hud-repo-rows")?.replaceChildren?.();
            }
            return;
          }
          const otherRow = repos.other && Number(repos.other.usd) > 0 ? repos.other : null;
          const sig = JSON.stringify([rows.map(r => [r.project, r.usd, r.tokens]), otherRow ? [otherRow.usd, otherRow.tokens] : null]);
          if (sig === lastReposSig) return;
          lastReposSig = sig;
          const top = Math.max(...rows.map(row => Number(row.usd) || 0));
          const line = (label, usdValue, tokens, extraClass) => {
            const item = document.createElement("div");
            item.className = "repo-row" + (extraClass ? " " + extraClass : "");
            const name = document.createElement("span");
            name.className = "repo-name";
            name.textContent = label;
            const value = document.createElement("span");
            value.className = "repo-value";
            value.textContent = usd2(usdValue) + " · " + tok(tokens || 0);
            const bar = document.createElement("span");
            bar.className = "repo-bar";
            const fill = document.createElement("i");
            fill.style.width = (top ? Math.max(2, (Number(usdValue) || 0) / top * 100) : 0) + "%";
            bar.append(fill);
            item.append(name, value, bar);
            return item;
          };
          const items = rows.map(row => line(row.project, row.usd, row.tokens));
          if (otherRow) items.push(line("Other", otherRow.usd, otherRow.tokens, "other"));
          $("hud-repo-rows").replaceChildren(...items);
        }

        function renderSummary(data) {
          statusView.summary = data.status || "live";
          statusView.failing = data.failingSource || "";
          statusView.offline = false;
          paintStatus();
          renderAgents(data);

          const groups = holdReadings(planGroups(data));
          groups.sort((a, b) => shownPeak(b) - shownPeak(a));
          const topGroup = groups.find(g => shownPeak(g) >= 0) || groups[0] || null;
          const miniBar = $("hud-mini-bar");
          if (topGroup) {
            const topRow = topGroup.rows.find(r => r.pct != null) || topGroup.rows[0];
            const until = topRow ? resetIn(topRow.resetsAt) : "";
            $("hud-mini-lead").textContent = topGroup.name + (topRow ? " · " + windowName(topGroup.key, topRow.label) : "") + (until ? " (" + until + ")" : "");
            $("hud-mini-pct").textContent = topRow && topRow.pct != null ? pctText(topRow.pct) : "—";
            miniBar.dataset.providerKey = topGroup.toolKey || topGroup.key;
          } else {
            $("hud-mini-lead").textContent = "All subscription limits healthy";
            $("hud-mini-pct").textContent = "—";
            miniBar.dataset.providerKey = "";
          }
          renderPlans(groups);
          feedHealth.live = Date.now();
          noteDataAge(data);
          paintUpdated();
          syncHudWindowSize();
          refreshTip();
        }

        function clearMachineView() {
          // A different selection is a different dataset. Do not label the
          // previous computer's values as this one's while its polls wait/fail.
          hideTip();
          windowTokens = {};
          usageView = {nav: {todayUsd: null, burnRatePerDay: null, todayTokens: null}, totals: {cacheReusePct: null}, importedAt: null};
          paintCoverage();
          closePanel("hud-coverage-panel");
          closePanel("hud-attention-panel");
          for (const id of ["hud-today", "hud-mini-today", "hud-burn-rate", "hud-cache", "hud-tokens"]) setStat(id, "—");
          renderAgents({activity: []});
          renderPlans([], true);
          lastReposSig = "empty";
          $("hud-repos").hidden = true;
          $("hud-repo-rows").replaceChildren();
          $("hud-mini-lead").textContent = "Waiting for subscription usage…";
          $("hud-mini-pct").textContent = "—";
          $("hud-mini-bar").dataset.providerKey = "";
          feedHealth.live = 0;
          feedHealth.usage = 0;
          feedHealth.dataAt = null;
          feedStartedAt = Date.now();
          statusView.summary = "loading";
          statusView.failing = "";
          statusView.offline = false;
          paintStatus();
          paintUpdated();
          syncHudWindowSize();
        }

        // Every poll is bounded: a hung request aborts and reports offline
        // instead of wedging the one-in-flight guard forever.
        const FETCH_TIMEOUT_MS = 12000;
        const hudFetch = async (url, options = {}) => {
          const controller = new AbortController();
          const timer = setTimeout(() => controller.abort(), FETCH_TIMEOUT_MS);
          try {
            const readOnly = String(options.method || "GET").toUpperCase() === "GET";
            for (let attempt = 0; ; attempt++) {
              const res = await fetch(url, {...options, cache: "no-store", signal: controller.signal});
              // Live meters and usage share one bounded backend read slot.
              // Their polling intervals coincide, so dropping every busy
              // response can starve usage indefinitely. Retry reads within
              // this request's existing deadline, without releasing its guard.
              if (res.status !== 503 || !readOnly || attempt >= 8) {
                if (!res.ok) throw new Error(String(res.status));
                return await res.json();
              }
              await res.body?.cancel();
              const retryHeader = res.headers?.get("Retry-After");
              const seconds = retryHeader == null ? NaN : Number(retryHeader);
              const delay = Math.max(100, Number.isFinite(seconds) && seconds >= 0 ? seconds * 1000 : 1000)
                + Math.round(Math.random() * 200);
              await new Promise((resolve, reject) => {
                let retryTimer;
                const abort = () => {
                  clearTimeout(retryTimer);
                  reject(new DOMException("HUD read aborted", "AbortError"));
                };
                if (controller.signal.aborted) { abort(); return; }
                controller.signal.addEventListener("abort", abort, {once: true});
                retryTimer = setTimeout(() => {
                  controller.signal.removeEventListener("abort", abort);
                  resolve();
                }, delay);
              });
            }
          } finally { clearTimeout(timer); }
        };
        let livePending = null, liveAgain = false, usagePending = false, usageAgain = false, machinesPending = false;
        let machineGeneration = 0;
        // One read at a time. A queued request (a manual poll, a stream
        // update) runs again once the current read finishes; timer ticks
        // never stack up behind a slow read.
        function refresh(queue = false) {
          if (livePending) {
            if (queue) liveAgain = true;
            return livePending;
          }
          livePending = (async () => {
            do {
              liveAgain = false;
              const machine = selectedMachine;
              const generation = machineGeneration;
              try {
                const data = await hudFetch("/api/spend/hud?machine=" + encodeURIComponent(machine || "all"));
                if (machine === selectedMachine && generation === machineGeneration) renderSummary(data);
              } catch {
                if (machine === selectedMachine && generation === machineGeneration) {
                  statusView.offline = true;
                  paintStatus();
                }
              }
            } while (liveAgain);
          })().finally(() => { livePending = null; });
          return livePending;
        }

        async function refreshUsage(queue = false) {
          if (usagePending) {
            if (queue) usageAgain = true;
            return;
          }
          usagePending = true;
          try {
            do {
              usageAgain = false;
              const machine = selectedMachine;
              const generation = machineGeneration;
              try {
                const data = await hudFetch("/api/spend/hud/usage?machine=" + encodeURIComponent(machine || "all"));
                if (machine === selectedMachine && generation === machineGeneration) {
                  feedHealth.usage = Date.now();
                  renderUsage(data);
                }
              } catch {}
            } while (usageAgain);
          } finally { usagePending = false; }
        }

        async function refreshMachines() {
          if (machinesPending) return;
          machinesPending = true;
          try {
            const data = await hudFetch("/api/machines");
            feedHealth.machines = Date.now(); renderMachines(data.machines || []); refreshTip();
          } catch {} finally { machinesPending = false; }
        }

        // This computer's own freshness (local only, never the hub's view):
        // a stalled source, a meter holding an old reading, a failing sync.
        const statusView = {summary: "loading", failing: "", offline: false, freshness: null};
        const ago = seconds => {
          const n = Number(seconds);
          if (seconds == null || !Number.isFinite(n)) return "";
          if (n < 90) return Math.round(n) + "s";
          if (n < 5400) return Math.round(n / 60) + "m";
          if (n < 172800) return Math.round(n / 3600) + "h";
          return Math.round(n / 86400) + "d";
        };
        let statusTip = "";
        function paintStatus() {
          const pill = $("hud-status-pill");
          const miniAlert = $("hud-mini-alert");
          const fresh = statusView.summary === "loading" ? null : statusView.freshness;
          const all = fresh && Array.isArray(fresh.issues) ? fresh.issues : [];
          const issues = all.filter(item => item.severity !== "info");
          const notes = all.filter(item => item.severity === "info");
          const lead = issues[0];
          const summary = statusView.offline ? "offline" : statusView.summary;
          let state = summary === "success" || summary === "live" ? "live" : summary === "offline" ? "error" : summary;
          let label = summary === "loading" ? "UPDATING" : summary === "offline" ? "OFFLINE" : summary === "stale" ? "STALE" : summary === "error" ? "ERR" : "LIVE";
          let title = summary === "loading" ? "Loading selected computer" : summary === "offline" ? "Offline" : summary === "stale" ? "Stale" : summary === "error" ? "Ingest failed" : "Live ingest";
          let body = summary === "offline"
            ? T.line("The local BURNRATE service is unreachable.", "dim")
            : summary === "loading"
              ? T.line("Waiting for this computer's usage readings.", "dim")
            : summary === "stale"
              ? T.line("Ingest has not refreshed on schedule" + (statusView.failing ? ": " + statusView.failing : "") + ".", "dim")
              : summary === "error"
                ? T.line("A source failed to collect" + (statusView.failing ? ": " + statusView.failing : "") + ".", "dim")
                : lead ? "" : T.line("Every source on this computer is collecting on schedule.", "dim");
          if (state === "live" && lead) {
            state = fresh.status === "alarm" ? "error" : "stale";
            label = (lead.short + " " + lead.state).toUpperCase() + (issues.length > 1 ? " +" + (issues.length - 1) : "");
            title = issues.length === 1 ? "1 source needs attention" : issues.length + " sources need attention";
          }
          let foot = "";
          if (!statusView.offline) {
            body += T.kv(issues.map(item => {
              const age = ago(item.ageSeconds);
              return [item.short, item.state + (age ? " " + age : ""), item.severity === "alarm" ? "dry" : "tight", item.detail];
            }));
            if (notes.length) body += T.sep + notes.map(item => T.line(item.detail, "dim")).join("");
            const beat = fresh && fresh.ingest;
            if (beat && beat.lastPassSeconds != null) {
              foot = "Last collection pass " + beat.lastPassSeconds + "s, " + (ago(beat.lastPassAgeSeconds) || "0s") + " ago";
            }
          }
          pill.dataset.state = state;
          $("hud-status-text").textContent = label;
          statusTip = T.head(title) + body + T.foot(foot);
          miniAlert.hidden = state === "live";
          miniAlert.dataset.state = state;
          miniAlert.textContent = state === "live" ? "" : issues.length
            ? issues.length + (issues.length === 1 ? " source needs attention" : " sources need attention")
            : summary === "loading" ? "Waiting for readings" : "Usage needs attention";
          miniAlert.disabled = summary === "loading";
          paintAttention();
          refreshTip();
        }
        setTip($("hud-status-pill"), () => statusTip);
        setTip($("hud-mini-alert"), () => statusTip);
        async function refreshFreshness() {
          try {
            statusView.freshness = await hudFetch("/api/spend/freshness?view=hud");
            feedHealth.freshness = Date.now();
          } catch {
            statusView.freshness = null;
          }
          paintStatus();
        }

        const panelOpeners = new Map();
        function panelTriggers(id) {
          return [...document.querySelectorAll("[aria-controls]")].filter(node => node.getAttribute("aria-controls") === id);
        }
        function closePanel(id) {
          const panel = $(id);
          if (!panel) return;
          const restore = panel.contains(document.activeElement);
          panel.hidden = true;
          panelTriggers(id).forEach(node => node.setAttribute("aria-expanded", "false"));
          if (restore) {
            const opener = panelOpeners.get(id);
            const density = $("hud-density-btn");
            const target = opener?.getClientRects().length ? opener
              : density?.getClientRects().length ? density : $("hud-mini-btn");
            target?.focus({preventScroll: true});
          }
          syncHudWindowSize();
        }
        function togglePanel(id, opener) {
          const panel = $(id);
          if (!panel) return;
          if (!panel.hidden) { closePanel(id); return; }
          panel.hidden = false;
          panelOpeners.set(id, opener);
          panelTriggers(id).forEach(node => node.setAttribute("aria-expanded", "true"));
          hideTip();
          panel.querySelector("button")?.focus({preventScroll: true});
          syncHudWindowSize();
        }
        function paintCoverage() {
          const nav = usageView.nav || {};
          const hasUsage = nav.todayUsd != null || Number(nav.todayTokens) > 0 || Number(nav.todayUnpricedTokens) > 0;
          const partial = nav.todayComplete === false && hasUsage;
          for (const id of ["hud-coverage-btn", "hud-mini-coverage-btn"]) {
            const button = $(id);
            if (!button) continue;
            button.hidden = !partial;
            button.textContent = nav.todayUsd == null ? "Unavailable" : "Partial";
            button.setAttribute("aria-label", "Explain today's pricing coverage");
          }
          const host = $("hud-coverage-detail");
          if (!host) return;
          const summary = document.createElement("p");
          summary.textContent = "API value is what recorded usage would cost at published API rates, not a bill.";
          const detail = document.createElement("p");
          detail.textContent = usageGapMessage(nav) || "All recorded usage in this total has a usable value.";
          const action = document.createElement("p");
          action.className = "coverage-help";
          action.textContent = partial ? "Missing rates or token details are excluded from the amount. Open the dashboard's Diagnostics for coverage by model."
            : "Recorded usage is valued separately from subscription payments.";
          host.replaceChildren(summary, detail, action);
          if (!partial) closePanel("hud-coverage-panel");
        }
        function attentionIssues() {
          const raw = statusView.summary === "loading" ? [] : statusView.freshness?.issues || [];
          const issues = raw.filter(item => item.severity !== "info");
          if (statusView.offline) return [{key: "offline", name: "Local service", state: "offline",
            detail: "The local BURNRATE service is unreachable. Start or reconnect the app, then retry."}];
          if (!issues.length && feedLate()) return [{key: "feed", name: "Usage readings", state: "stale",
            ageSeconds: dataAgeSeconds(), detail: "A usage poll has not refreshed on schedule. Retry to request a fresh reading."}];
          if (!issues.length && ["error", "stale"].includes(statusView.summary)) return [{key: "source", name: statusView.failing || "Usage collection", state: statusView.summary,
            detail: "A source has not refreshed successfully. Recheck it or open source settings."}];
          return issues;
        }
        function paintAttention() {
          const host = $("hud-attention-content");
          if (!host) return;
          const focused = document.activeElement?.dataset.attentionFocus;
          const issues = attentionIssues();
          if (!issues.length) {
            const line = document.createElement("p");
            line.textContent = statusView.summary === "loading" ? "Waiting for this computer's readings." : "All sources are collecting on schedule.";
            host.replaceChildren(line);
            if (focused) $("hud-attention-close")?.focus({preventScroll: true});
            return;
          }
          host.replaceChildren(...issues.map((issue, index) => {
            const article = document.createElement("article");
            article.className = "hud-issue";
            const title = document.createElement("strong");
            title.textContent = issue.name || issue.short || "Usage source";
            const stamp = document.createElement("p");
            const age = ago(issue.ageSeconds);
            stamp.textContent = issue.state + (age ? " · last successful reading " + age + " ago" : " · no recent successful reading");
            const detail = document.createElement("p");
            detail.textContent = issue.detail || "This source needs a fresh reading.";
            const next = document.createElement("p");
            next.className = "issue-next";
            next.textContent = issue.nextAction || (issue.key === "hub-upgrade" ? "Update the hub; queued corrections will sync after it reconnects."
              : issue.kind === "quota" ? "Check the provider is signed in, then recheck the reading."
              : issue.kind === "sync" ? "Check the hub connection, then retry."
              : "Recheck the source. If it stays unavailable, open source settings.");
            const retry = document.createElement("button");
            retry.type = "button";
            retry.className = "issue-action";
            retry.dataset.attentionFocus = String(issue.key || index);
            retry.textContent = "Recheck";
            retry.addEventListener("click", () => {
              if (issue.kind === "quota") $("hud-poll-btn")?.click();
              refresh(true); refreshUsage(true); refreshFreshness();
              notice("Checking the latest readings…");
            });
            const setup = document.createElement("button");
            setup.type = "button";
            setup.className = "issue-action";
            setup.dataset.attentionFocus = "setup:" + String(issue.key || index);
            setup.textContent = "Source settings";
            setup.addEventListener("click", () => $("hud-setup-btn").click());
            article.append(title, stamp, detail, next, retry, setup);
            return article;
          }));
          if (focused) [...host.querySelectorAll("[data-attention-focus]")].find(node => node.dataset.attentionFocus === focused)?.focus({preventScroll: true});
        }
        for (const id of ["hud-coverage-btn", "hud-mini-coverage-btn"]) $(id)?.addEventListener("click", event => {
          paintCoverage(); togglePanel("hud-coverage-panel", event.currentTarget);
        });
        for (const id of ["hud-mini-alert", "hud-status-pill"]) $(id)?.addEventListener("click", event => {
          paintAttention(); togglePanel("hud-attention-panel", event.currentTarget);
        });
        $("hud-coverage-close")?.addEventListener("click", () => closePanel("hud-coverage-panel"));
        $("hud-attention-close")?.addEventListener("click", () => closePanel("hud-attention-panel"));
        $("hud-context-disclosure")?.addEventListener("toggle", () => syncHudWindowSize());
        document.addEventListener("keydown", event => {
          if (event.key !== "Escape") return;
          const moreMenu = $("hud-more-menu");
          if (moreMenu && !moreMenu.hidden) {
            event.preventDefault();
            closeMoreMenu(true);
            return;
          }
          for (const id of ["hud-coverage-panel", "hud-attention-panel"]) if ($(id) && !$(id).hidden) {
            event.preventDefault(); closePanel(id);
          }
        });
        paintCoverage();

        async function checkSuggestedPlan() {
          const banner = $("hud-quick-plan");
          if (!banner) return;
          try {
            const data = await hudFetch("/api/setup/check");
            const suggestions = Array.isArray(data?.plans?.suggestions) ? data.plans.suggestions : null;
            if (!suggestions) return;
            let dismissed = [];
            try {
              const raw = JSON.parse(localStorage.getItem("burnrate:hud-dismissed-plans") || "[]");
              if (Array.isArray(raw)) dismissed = raw.filter(k => typeof k === "string");
            } catch {}
            const sug = suggestions.find(item =>
              item && item.lane && item.tool_key && item.name &&
              item.amount_usd != null && Number.isFinite(Number(item.amount_usd)) &&
              !dismissed.includes(String(item.lane))
            );
            if (!sug) {
              if (!banner.hidden) {
                banner.hidden = true;
                banner.replaceChildren();
                syncHudWindowSize();
              }
              return;
            }
            const copy = document.createElement("span");
            copy.className = "quick-plan-copy";
            const strong = document.createElement("b");
            strong.textContent = sug.name;
            const amount = Number(sug.amount_usd);
            const priceLabel = Number.isInteger(amount) ? "$" + amount : "$" + amount.toFixed(2);
            copy.append(strong, " · " + priceLabel + "/mo detected");

            const actions = document.createElement("div");
            actions.className = "quick-plan-actions";
            const confirmBtn = document.createElement("button");
            confirmBtn.type = "button";
            confirmBtn.className = "quick-plan-confirm";
            confirmBtn.textContent = "Confirm";
            confirmBtn.addEventListener("click", async () => {
              confirmBtn.disabled = true;
              confirmBtn.textContent = "Saving…";
              const now = new Date();
              const monthStart = now.getFullYear() + "-" + String(now.getMonth() + 1).padStart(2, "0") + "-01";
              try {
                const res = await fetch("/api/subscriptions", {
                  method: "POST",
                  headers: {
                    "Content-Type": "application/json",
                    "X-BURNRATE-Request": "1"
                  },
                  body: JSON.stringify({
                    operation: "add",
                    request_id: "hud-plan-" + Date.now() + "-" + Math.random().toString(36).slice(2, 10),
                    values: {
                      name: String(sug.name),
                      tool_key: String(sug.tool_key),
                      amount_usd: amount.toFixed(2),
                      cadence: String(sug.cadence || "monthly"),
                      start_date: monthStart,
                      end_date: null
                    }
                  })
                });
                if (!res.ok) throw new Error(String(res.status));
                banner.hidden = true;
                banner.replaceChildren();
                notice("Added " + sug.name + " (" + priceLabel + "/mo)");
                refresh(true);
                refreshUsage(true);
                syncHudWindowSize();
                setTimeout(() => { if (!document.hidden) checkSuggestedPlan(); }, 250);
              } catch {
                confirmBtn.disabled = false;
                confirmBtn.textContent = "Confirm";
                notice("Could not save plan · open Setup to configure");
              }
            });

            const dismissBtn = document.createElement("button");
            dismissBtn.type = "button";
            dismissBtn.className = "quick-plan-dismiss";
            dismissBtn.setAttribute("aria-label", "Dismiss plan suggestion");
            dismissBtn.textContent = "×";
            dismissBtn.addEventListener("click", () => {
              const nextDismissed = [...new Set([...dismissed, String(sug.lane)])];
              try { localStorage.setItem("burnrate:hud-dismissed-plans", JSON.stringify(nextDismissed)); } catch {}
              banner.hidden = true;
              banner.replaceChildren();
              syncHudWindowSize();
              setTimeout(() => { if (!document.hidden) checkSuggestedPlan(); }, 100);
            });

            actions.append(confirmBtn, dismissBtn);
            banner.replaceChildren(copy, actions);
            banner.hidden = false;
            syncHudWindowSize();
          } catch {}
        }

        refresh();
        refreshUsage();
        refreshMachines();
        refreshFreshness();
        setTimeout(() => { if (!document.hidden) checkSuggestedPlan(); }, 700);
        setInterval(() => { if (!document.hidden) paintUpdated(); }, 1000);
        setInterval(() => { if (!document.hidden) refresh(); }, 5000);
        setInterval(() => { if (!document.hidden) refreshUsage(); }, 20000);
        setInterval(() => { if (!document.hidden) refreshMachines(); }, 20000);
        setInterval(() => { if (!document.hidden) refreshFreshness(); }, 20000);
        window.addEventListener("focus", () => { refresh(); refreshUsage(); refreshMachines(); refreshFreshness(); });
        document.addEventListener("visibilitychange", () => {
          if (!document.hidden) {
            paintUpdated();
            refresh();
            refreshUsage();
            refreshMachines();
            refreshFreshness();
          }
        });
        if (typeof EventSource === "function") {
          let debounce = null;
          try {
            const es = new EventSource("/api/spend/stream");
            es.addEventListener("update", () => {
              if (document.hidden) return;
              if (debounce) clearTimeout(debounce);
              debounce = setTimeout(() => { debounce = null; refresh(true); }, 100);
            });
          } catch {}
        }
      })();
