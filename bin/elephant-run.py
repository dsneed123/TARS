"""Run Elephant backend with proper signal handling."""
import asyncio
import signal
import sys
import uvicorn

def main():
    config = uvicorn.Config(
        "app.main:app",
        host="0.0.0.0",
        port=8100,
        log_level="info",
        loop="asyncio",
    )
    server = uvicorn.Server(config)

    # Handle signals gracefully
    def handle_signal(sig, frame):
        server.should_exit = True

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    server.run()

if __name__ == "__main__":
    main()
