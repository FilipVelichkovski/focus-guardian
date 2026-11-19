// KWin 6 script: pin the Focus Guardian Sticky Note above the desktop (keepBelow),
// prevent minimizing, and hide it from taskbar/pager/Alt-Tab.

const TARGET_CAPTION = "Focus Guardian Sticky Note";

function applyStickyRules(win) {
    if (!win || String(win.caption || "") !== TARGET_CAPTION) {
        return;
    }
    try {
        win.keepBelow = true;
        win.keepAbove = false;
        win.noBorder = true;
        win.skipTaskbar = true;
        win.skipPager = true;
        win.skipSwitcher = true;
        win.onAllDesktops = true;
        if (win.minimized) {
            win.minimized = false;
        }
        const area = workspace.clientArea(KWin.PlacementArea, win);
        const geo = win.frameGeometry;
        win.frameGeometry = {
            x: Math.round(area.x + area.width - geo.width - 24),
            y: Math.round(area.y + 24),
            width: geo.width,
            height: geo.height
        };
    } catch (e) {
        // Ignore read-only properties on some window types
    }
}

function attachSticky(win) {
    if (!win || String(win.caption || "") !== TARGET_CAPTION) {
        return;
    }
    applyStickyRules(win);
    try {
        win.minimizedChanged.connect(function() {
            if (win.minimized) {
                win.minimized = false;
            }
        });
        win.keepBelowChanged.connect(function() {
            if (!win.keepBelow) {
                win.keepBelow = true;
            }
        });
    } catch (e) {
        // Ignore if signal connection fails
    }
}

workspace.windowAdded.connect(attachSticky);

// Apply to any already-open sticky note window
const wins = workspace.windowList();
for (let i = 0; i < wins.length; i++) {
    attachSticky(wins[i]);
}
