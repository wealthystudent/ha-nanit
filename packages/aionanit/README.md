# aionanit

Async Python client library for Nanit baby cameras.

## Features

- **Authentication**: Email/password login, MFA verification, automatic token refresh.
- **WebSocket**: Protobuf-over-WebSocket communication with cameras (cloud and local).
- **REST API**: Baby metadata, cloud events, snapshots.
- **Streaming**: RTMPS URL construction for live video.
- **Push-based**: Subscribe to real-time camera state changes (sensors, settings, controls).

## Installation

```bash
pip install aionanit
```

## Quick Start

```python
import aiohttp
from aionanit import NanitClient

async with aiohttp.ClientSession() as session:
    client = NanitClient(session)

    # Login
    tokens = await client.async_login("you@example.com", "password")

    # Get babies
    babies = await client.async_get_babies()
    baby = babies[0]

    # Connect to camera
    camera = client.camera(baby.camera_uid, baby.uid)
    await camera.async_start()

    # Subscribe to state changes
    def on_event(event):
        print(f"Sensors: {event.state.sensors}")

    unsub = camera.subscribe(on_event)

    # Get RTMPS stream URL
    url = await camera.async_get_stream_rtmps_url()
    print(f"Stream: {url}")

    # Cleanup
    unsub()
    await client.async_close()
```

## Requirements

- Python 3.12+
- aiohttp >= 3.9.0
- protobuf >= 6.0

## License

MIT
