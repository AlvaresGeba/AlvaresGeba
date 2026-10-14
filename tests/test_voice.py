import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import httpx
import numpy as np
from fastapi.testclient import TestClient

import gateway
import stt_service
from command_router import CommandOutcome
from history import History
from stt import SAMPLE_RATE, AudioError, Settings, Transcriber, pcm16_to_float32


def make_pcm(seconds: float, amplitude: float = 0.2, freq: float = 220.0) -> bytes:
    """A sine wave as raw s16le mono 16 kHz - loud enough to pass the silence check."""
    t = np.arange(int(seconds * SAMPLE_RATE)) / SAMPLE_RATE
    wave = amplitude * np.sin(2 * np.pi * freq * t)
    return (wave * 32767).astype("<i2").tobytes()


class FakeModel:
    """Stands in for faster_whisper.WhisperModel."""

    def __init__(self, texts=("статус сервера",)):
        self.texts = texts
        self.calls = []

    def transcribe(self, audio, **kwargs):
        self.calls.append((audio, kwargs))
        segments = (SimpleNamespace(text=f" {t} ") for t in self.texts)
        return segments, SimpleNamespace(language="ru", language_probability=0.99)


def make_transcriber(texts=("статус сервера",)) -> tuple[Transcriber, FakeModel]:
    model = FakeModel(texts)
    return Transcriber(Settings(), model=model), model


class Pcm16Tests(unittest.TestCase):
    def test_converts_to_float32_range(self) -> None:
        raw = np.array([0, 16384, -32768, 32767], dtype="<i2").tobytes()
        audio = pcm16_to_float32(raw)
        self.assertEqual(audio.dtype, np.float32)
        np.testing.assert_allclose(audio, [0.0, 0.5, -1.0, 32767 / 32768], atol=1e-6)

    def test_odd_byte_count_is_rejected(self) -> None:
        with self.assertRaises(AudioError):
            pcm16_to_float32(b"\x00\x01\x02")


class TranscriberTests(unittest.TestCase):
    def test_returns_joined_stripped_text(self) -> None:
        transcriber, model = make_transcriber(("покажи", "диск"))
        self.assertEqual(transcriber.transcribe(make_pcm(1.0)), "покажи диск")
        _, kwargs = model.calls[0]
        self.assertEqual(kwargs["language"], "ru")
        self.assertTrue(kwargs["vad_filter"])
        self.assertEqual(kwargs["beam_size"], 1)

    def test_silence_is_rejected_without_calling_the_model(self) -> None:
        transcriber, model = make_transcriber()
        with self.assertRaisesRegex(AudioError, "silent"):
            transcriber.transcribe(b"\x00\x00" * SAMPLE_RATE)
        self.assertEqual(model.calls, [])

    def test_too_short_and_too_long_are_rejected(self) -> None:
        transcriber, _ = make_transcriber()
        with self.assertRaisesRegex(AudioError, "short"):
            transcriber.transcribe(make_pcm(0.1))
        with self.assertRaisesRegex(AudioError, "long"):
            transcriber.transcribe(make_pcm(11.0))

    def test_no_speech_gives_empty_string(self) -> None:
        transcriber, _ = make_transcriber(texts=())
        self.assertEqual(transcriber.transcribe(make_pcm(1.0)), "")


class SttServiceTests(unittest.TestCase):
    """stt_service.py: the HTTP wrapper around Transcriber (whisper container)."""

    def post(self, body: bytes, transcriber: Transcriber):
        with mock.patch.object(stt_service, "get_transcriber", return_value=transcriber):
            client = TestClient(stt_service.app)  # no `with`: skip the warm-up lifespan
            return client.post("/transcribe", content=body)

    def test_returns_text(self) -> None:
        transcriber, _ = make_transcriber(("статус сервера",))
        response = self.post(make_pcm(2.0), transcriber)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {"text": "статус сервера"})

    def test_silent_audio_is_400(self) -> None:
        transcriber, _ = make_transcriber()
        response = self.post(b"\x00\x00" * SAMPLE_RATE, transcriber)
        self.assertEqual(response.status_code, 400)
        self.assertIn("silent", response.json()["detail"])

    def test_oversized_body_is_413(self) -> None:
        transcriber, model = make_transcriber()
        response = self.post(make_pcm(12.0), transcriber)
        self.assertEqual(response.status_code, 413)
        self.assertEqual(model.calls, [])


def use_temp_history(test: unittest.TestCase) -> History:
    tmp = tempfile.TemporaryDirectory()
    test.addCleanup(tmp.cleanup)
    history = History(Path(tmp.name) / "history.db")
    patcher = mock.patch.object(gateway, "_history", history)
    patcher.start()
    test.addCleanup(patcher.stop)
    return history


class VoiceEndpointTests(unittest.TestCase):
    """gateway.py /command/audio with a fake STT service behind httpx."""

    def setUp(self) -> None:
        self.client = TestClient(gateway.app)
        self.history = use_temp_history(self)

    def post(self, body: bytes, stt_handler, router_result="OK: done", query="&lang=ru"):
        """`stt_handler` plays the stt container: httpx.Request -> httpx.Response."""
        router = mock.AsyncMock(return_value=CommandOutcome(router_result, router_result + "\n\nfull"))
        real_client = httpx.AsyncClient
        fake_client = lambda **kw: real_client(transport=httpx.MockTransport(stt_handler), **kw)
        with mock.patch.object(gateway.httpx, "AsyncClient", fake_client), \
                mock.patch.object(gateway, "handle_text_command", router):
            response = self.client.post(
                "/command/audio?device_id=watch-01" + query,
                content=body,
                headers={"content-type": "application/octet-stream"},
            )
        return response, router

    def test_audio_goes_through_stt_then_router(self) -> None:
        sent = []

        def stt(request: httpx.Request) -> httpx.Response:
            sent.append(request)
            return httpx.Response(200, json={"text": "статус сервера"})

        pcm = make_pcm(2.0)
        response, router = self.post(pcm, stt)

        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["transcript"], "статус сервера")
        self.assertEqual(body["result"], "OK: done")
        self.assertEqual(body["detail"], "OK: done\n\nfull")
        self.assertIsNone(body["error"])
        self.assertEqual(router.await_args.args[0], "статус сервера")
        self.assertEqual(str(sent[0].url), f"{gateway.STT_URL}/transcribe?language=ru")
        self.assertEqual(sent[0].content, pcm)

        [entry] = self.history.recent()
        self.assertEqual((entry["channel"], entry["device_id"], entry["ok"]), ("voice", "watch-01", 1))

    def test_no_lang_means_autodetect(self) -> None:
        sent = []

        def stt(request: httpx.Request) -> httpx.Response:
            sent.append(request)
            return httpx.Response(200, json={"text": "hello"})

        self.post(make_pcm(2.0), stt, query="")
        self.assertEqual(sent[0].url.params["language"], "auto")

    def test_unrecognised_speech_does_not_reach_router(self) -> None:
        response, router = self.post(make_pcm(2.0), lambda r: httpx.Response(200, json={"text": ""}))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["transcript"], "")
        self.assertTrue(response.json()["result"].startswith("ERROR"))
        router.assert_not_awaited()

    def test_audio_rejected_by_stt_is_400(self) -> None:
        response, router = self.post(
            make_pcm(2.0), lambda r: httpx.Response(400, json={"detail": "Audio is silent"})
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["detail"], "Audio is silent")
        router.assert_not_awaited()

    def test_stt_down_is_500(self) -> None:
        def stt(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused", request=request)

        response, router = self.post(make_pcm(2.0), stt)

        self.assertEqual(response.status_code, 500)
        router.assert_not_awaited()

    def test_oversized_body_is_413(self) -> None:
        stt = mock.Mock()
        response, router = self.post(make_pcm(12.0), stt)

        self.assertEqual(response.status_code, 413)
        stt.assert_not_called()
        router.assert_not_awaited()

    def test_router_failure_is_reported_and_recorded(self) -> None:
        router = mock.AsyncMock(side_effect=RuntimeError("boom"))
        with mock.patch.object(gateway, "transcribe_audio", mock.AsyncMock(return_value="диск")), \
                mock.patch.object(gateway, "handle_text_command", router):
            response = self.client.post("/voice", content=make_pcm(1.0))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["error"], "boom")
        self.assertTrue(response.json()["result"].startswith("ERROR"))
        [entry] = self.history.recent(failures=True)
        self.assertEqual(entry["transcript"], "диск")


if __name__ == "__main__":
    unittest.main()
