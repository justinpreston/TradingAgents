"""Custom data reader for TradingAgents ``signals.json`` manifests.

The algorithm primarily loads ``signals.json`` directly during ``Initialize``
because a weekly trade manifest is configuration, not a price series. This
``PythonData`` class is provided for users who prefer to host or stream the
same manifest through LEAN's custom-data subscription path. It expects either a
single JSON object matching the TradingAgents schema or newline-delimited JSON
objects with that same shape.
"""

import json
from datetime import datetime, timezone

from AlgorithmImports import *


class TradingAgentsSignalData(PythonData):
    """One custom-data record containing a TradingAgents signal manifest."""

    def GetSource(self, config, date, isLiveMode):
        source = getattr(config, "Source", None) or getattr(config.Symbol, "Value", "signals.json")
        transport = SubscriptionTransportMedium.RemoteFile if str(source).startswith(("http://", "https://")) else SubscriptionTransportMedium.LocalFile
        return SubscriptionDataSource(source, transport)

    def Reader(self, config, line, date, isLiveMode):
        if not line or not line.strip():
            return None

        try:
            payload = json.loads(line)
        except ValueError:
            return None

        generated_at = payload.get("generated_at") or payload.get("measure_date")
        record_time = self._parse_time(generated_at) or date

        data = TradingAgentsSignalData()
        data.Symbol = config.Symbol
        data.Time = record_time
        data.EndTime = record_time
        data.Value = float(len(payload.get("signals", [])))
        data["schema_version"] = payload.get("schema_version", "")
        data["source_run"] = payload.get("source_run", "")
        data["measure_date"] = payload.get("measure_date", "")
        data["signals"] = payload.get("signals", [])
        return data

    @staticmethod
    def _parse_time(value):
        if not value:
            return None
        if isinstance(value, datetime):
            return value
        text = str(value).replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
        if parsed.tzinfo is not None:
            return parsed.astimezone(timezone.utc).replace(tzinfo=None)
        return parsed
