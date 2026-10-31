"""Undo, deliberately narrow: only effects FocusFlow can genuinely reverse.

  settings  - restore previous setting values
  enabled   - restore the previous ON/OFF state
Clicks FocusFlow makes in other applications are never offered for undo: it cannot know what they did.
(Earlier versions could also close programs they had launched; launching was removed with Study Mode, and the
database migration disarms any such stored records.)
"""


class Undo:
    def __init__(self, cfg, store, app):
        self.cfg, self.store, self.app = cfg, store, app

    def undo(self, event_id):
        """Returns (ok, message)."""
        ev = self.store.get(event_id)
        if ev is None:
            return False, "No such event."
        if ev["undone"]:
            return False, "Already undone."
        undo = ev["undo"]
        if not undo:
            return False, "FocusFlow cannot safely reverse this action."
        kind = undo.get("type")
        if kind == "settings":
            self.cfg.set_many(undo["previous"])
            ok, msg = True, "Setting restored."
        elif kind == "enabled":
            self.app.set_enabled(bool(undo["previous"]))
            ok, msg = True, "Restored."
        else:
            ok, msg = False, "FocusFlow cannot safely reverse this action."
        if ok:
            self.store.mark_undone(event_id)
        return ok, msg
