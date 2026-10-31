"""Voice commands through the Windows built-in speech recogniser (SAPI), running entirely on this PC.

A fixed command grammar only: it cannot transcribe free speech, nothing is recorded or stored, and no audio
leaves the machine. Needs a Windows speech-recognition language and a microphone; if either is missing the
listener reports why instead of pretending to work."""
import os
import tempfile
import threading
from xml.sax.saxutils import escape

# spoken phrase -> action name understood by App.voice_action()
COMMANDS = {
    "pause focusflow": "pause",
    "resume focusflow": "resume",
    "stop assistance": "emergency",
    "click": "click",
    "double click": "double_click",
    "right click": "right_click",
    "scroll up": "scroll_up",
    "scroll down": "scroll_down",
    "next profile": "next_profile",
    "precision mode": "profile:Precision",
    "comfort mode": "profile:Comfort",
    "maximum assistance": "profile:Maximum Assistance",
    "show keyboard": "keyboard_show",
    "hide keyboard": "keyboard_hide",
    "open pointer lab": "open_lab",
    "open focusflow": "open_window",
}
SLO_DYNAMIC, SGDS_ACTIVE = 1, 1
MIN_CONFIDENCE = 0      # SAPI ActualConfidence: -1 low, 0 normal, 1 high. Low-confidence results are dropped.


def grammar_xml(phrases=COMMANDS, langid="409"):
    items = "\n".join(f"      <P>{escape(p)}</P>" for p in phrases)
    return f'<GRAMMAR LANGID="{langid}">\n  <RULE NAME="cmds" TOPLEVEL="ACTIVE">\n    <L>\n{items}\n    </L>\n  </RULE>\n</GRAMMAR>'


class VoiceListener(threading.Thread):
    def __init__(self, handler, on_status=None):
        super().__init__(daemon=True, name="ff-voice")
        self.handler = handler                  # handler(action, phrase)
        self.on_status = on_status
        self.status = "starting"
        self.heard = []                         # last recognised phrases (for the UI)
        self._halt = threading.Event()
        self.finished = threading.Event()

    def _set(self, status):
        self.status = status
        if self.on_status:
            try:
                self.on_status(status)
            except Exception:
                pass

    def stop(self):
        self._halt.set()

    def run(self):
        try:
            import comtypes
            import comtypes.client as cc
            comtypes.CoInitialize()
        except Exception as exc:
            self._set(f"unavailable: {exc}")
            return
        try:
            reco = cc.CreateObject("SAPI.SpInProcRecognizer")
            cat = cc.CreateObject("SAPI.SpObjectTokenCategory")
            cat.SetId(r"HKEY_LOCAL_MACHINE\SOFTWARE\Microsoft\Speech\AudioInput")
            tokens = cat.EnumerateTokens()
            if tokens.Count == 0:
                self._set("unavailable: no microphone found")
                return
            reco.AudioInput = tokens.Item(0)
            try:                                     # the installed recogniser's language, e.g. "809" (English-UK)
                langid = reco.Recognizer.GetAttribute("Language").split(";")[0]
            except Exception:
                langid = "409"
            ctx = reco.CreateRecoContext()
            gram = ctx.CreateGrammar()
            path = os.path.join(tempfile.mkdtemp(prefix="ffvoice"), "commands.xml")
            with open(path, "w", encoding="utf-8") as f:
                f.write(grammar_xml(langid=langid))
            gram.CmdLoadFromFile(path, SLO_DYNAMIC)
            gram.CmdSetRuleState("cmds", SGDS_ACTIVE)
            listener = self

            class Sink:
                def Recognition(self, StreamNumber, StreamPosition, RecognitionType, Result):
                    listener._on_result(Result)

                def Hypothesis(self, *a):
                    pass

            sapi = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "Speech", "Common", "sapi.dll")
            cc.GetModule(sapi)                        # generates the typelib wrapper that names the event interface
            from comtypes.gen import SpeechLib
            self._events = cc.GetEvents(ctx, Sink(), interface=SpeechLib._ISpeechRecoContextEvents)
            self._set("listening")
            while not self._halt.is_set():
                cc.PumpEvents(0.1)
        except Exception as exc:                     # no recogniser / language pack / microphone busy ...
            self._set(f"unavailable: {str(exc)[:80]}")
        finally:
            if not self.status.startswith("unavailable"):
                self._set("stopped")
            self.finished.set()

    def _on_result(self, result):
        try:
            info = result.PhraseInfo
            text = info.GetText().strip().lower()
            elements = info.Elements
            if any(elements.Item(i).ActualConfidence < MIN_CONFIDENCE for i in range(elements.Count)):
                return
        except Exception:
            return
        action = COMMANDS.get(text)
        if action is None:
            return
        self.heard = (self.heard + [text])[-6:]
        try:
            self.handler(action, text)
        except Exception:
            pass


def available():
    """(ok, message) without starting a listener."""
    try:
        import comtypes
        import comtypes.client as cc
        comtypes.CoInitialize()
        for key, msg in (("Recognizers", "No Windows speech recogniser is installed"),
                         ("AudioInput", "No microphone found")):
            cat = cc.CreateObject("SAPI.SpObjectTokenCategory")      # SetId works once per object
            cat.SetId(rf"HKEY_LOCAL_MACHINE\SOFTWARE\Microsoft\Speech\{key}")
            if cat.EnumerateTokens().Count == 0:
                return False, msg
        return True, "Windows speech recogniser and microphone found"
    except Exception as exc:
        return False, f"Speech recognition unavailable: {exc}"
