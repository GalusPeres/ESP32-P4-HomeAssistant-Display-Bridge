"""Exercise delayed provider readiness and refresh cleanup with real Bridge methods."""
import asyncio
from datetime import timedelta
import json
from types import MethodType, SimpleNamespace
import unittest

import test_weather_forecasts as forecasts


class WeatherRefreshTests(unittest.IsolatedAsyncioTestCase):
    def runtime(self, responses):
        runtime, state, calls = forecasts.WeatherForecastTests().runtime(responses)
        scope = runtime._build_weather_payload.__func__.__globals__
        published, timers = [], []

        async def publish(hass, topic, payload, **kwargs):
            published.append(json.loads(payload))

        def call_later(hass, delay, callback):
            timer = SimpleNamespace(delay=delay, callback=callback, cancelled=False)
            timers.append(timer)
            def cancel():
                timer.cancelled = True
            return cancel

        scope.update(mqtt=SimpleNamespace(async_publish=publish), async_call_later=call_later)
        runtime._runtime_setup_complete = True
        runtime._weather_refresh_handle = None
        runtime._weather_refresh_task = None
        runtime._unsub_weather_stop = None
        runtime._weather_last_payload = {}
        runtime.weathers = [state.entity_id]
        runtime._owns_state_publish = lambda _: True
        runtime._ha_topic_for_entity = lambda entity, suffix: f"ha/{entity}/{suffix}"
        runtime.hass.async_create_task = asyncio.create_task
        for name in ("_schedule_weather_refresh", "_async_refresh_weather",
                     "_async_stop_weather_refresh", "_async_publish_weather_state"):
            setattr(runtime, name, MethodType(scope[name], runtime))
        return runtime, state, calls, published, timers, scope

    async def tick(self, runtime, timers):
        timer = timers[-1]
        self.assertFalse(timer.cancelled)
        timer.callback(None)
        await runtime._weather_refresh_task

    async def test_late_forecast_recovers_without_state_event_and_refreshes_after_ttl(self):
        responses = {}
        runtime, state, calls, published, timers, scope = self.runtime(responses)
        await runtime._async_publish_weather_state(state.entity_id, state)
        self.assertNotIn("forecast", published[-1])
        runtime._schedule_weather_refresh()
        runtime._schedule_weather_refresh()
        self.assertEqual(len(timers), 1)
        self.assertEqual(timers[0].delay, 60)
        await self.tick(runtime, timers)
        self.assertEqual(len(published), 1, "Do not republish the unchanged empty state")
        responses.update(forecasts.yandex_forecasts())
        await self.tick(runtime, timers)
        self.assertEqual(len(published[-1]["forecast"]), 7)
        self.assertEqual(len(published[-1]["forecast_hourly"]), 30)
        count = len(calls)
        await self.tick(runtime, timers)
        self.assertEqual(len(calls), count, "Keep successful forecast requests cached")
        self.assertEqual(len(published), 2, "Do not send unchanged forecast packets")
        now = scope["dt_util"].utcnow()
        scope["dt_util"].utcnow = lambda: now + timedelta(minutes=11)
        responses["twice_daily"][1]["temperature"] = 31
        await self.tick(runtime, timers)
        self.assertEqual(published[-1]["forecast"][0]["temperature"], 31)
        self.assertEqual(len(published), 3)

    async def test_only_owner_publishes_and_late_entity_can_recover(self):
        runtime, state, calls, published, timers, _ = self.runtime(forecasts.yandex_forecasts())
        runtime._owns_state_publish = lambda _: False
        runtime._schedule_weather_refresh()
        await self.tick(runtime, timers)
        self.assertFalse(calls)
        runtime._owns_state_publish = lambda _: True
        runtime.hass.states.get = lambda _: None
        await self.tick(runtime, timers)
        self.assertFalse(calls)
        runtime.hass.states.get = lambda _: state
        await self.tick(runtime, timers)
        self.assertEqual(len(published[-1]["forecast"]), 7)

    async def test_publication_failure_retries_and_unload_cancels_timer(self):
        runtime, state, calls, published, timers, scope = self.runtime(forecasts.yandex_forecasts())
        publish = scope["mqtt"].async_publish
        async def fail(*args, **kwargs):
            raise RuntimeError("MQTT unavailable")
        scope["mqtt"].async_publish = fail
        runtime._schedule_weather_refresh()
        await self.tick(runtime, timers)
        self.assertFalse(runtime._weather_last_payload)
        scope["mqtt"].async_publish = publish
        await self.tick(runtime, timers)
        self.assertEqual(len(published), 1)
        stopped = []
        runtime._unsub_weather_stop = lambda: stopped.append(True)
        await runtime._async_stop_weather_refresh()
        self.assertEqual(stopped, [True])
        self.assertFalse(runtime._runtime_setup_complete)
        self.assertTrue(timers[-1].cancelled)
        timers[-1].callback(None)
        self.assertIsNone(runtime._weather_refresh_task)

    async def test_unload_cancels_inflight_request_and_does_not_rearm(self):
        runtime, state, calls, published, timers, _ = self.runtime({})
        started = asyncio.Event()
        async def blocked(*args, **kwargs):
            started.set()
            await asyncio.Event().wait()
        runtime.hass.services.async_call = blocked
        runtime._schedule_weather_refresh()
        timers[-1].callback(None)
        await started.wait()
        runtime._runtime_setup_complete = False
        await runtime._async_stop_weather_refresh()
        self.assertIsNone(runtime._weather_refresh_task)
        self.assertEqual(len(timers), 1)
        self.assertFalse(published)


if __name__ == "__main__":
    unittest.main()
