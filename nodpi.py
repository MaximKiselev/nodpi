import asyncio
import random
import logging
from asyncio import StreamReader, StreamWriter
from typing import List

# Constants
PORT = 8881  # Port number for the proxy server to listen on
BLOCKED_FILE = "russia-blacklist.txt"  # Path to the file containing blocked sites
BUFFER_SIZE = 1500  # Size of the buffer for reading and writing data
TIMEOUT = None  # Timeout in seconds for reading and writing operations

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

try:
    import uvloop
    uvloop_available = True
except ImportError:
    uvloop_available = False

async def keep_alive() -> None:
    """A dummy coroutine to keep the event loop active."""
    while True:
        await asyncio.sleep(1)

async def main() -> None:
    """Start the proxy server and listen for incoming connections."""
    server = await asyncio.start_server(new_conn, '0.0.0.0', PORT)
    logging.info(f'Proxy server is running on 127.0.0.1:{PORT}')

    # Add a keep-alive task to prevent the event loop from idling
    keep_alive_task = asyncio.create_task(keep_alive())

    try:
        async with server:
            await asyncio.Future()  # Run until cancelled
    except asyncio.CancelledError:
        logging.info("Server shutting down...")
    finally:
        keep_alive_task.cancel()
        server.close()
        await server.wait_closed()
        logging.info("Server has been shut down.")

async def pipe(reader: StreamReader, writer: StreamWriter) -> None:
    """Transfer data between reader and writer with timeouts.

    Args:
        reader (StreamReader): The input stream reader.
        writer (StreamWriter): The output stream writer.
    """
    try:
        while not reader.at_eof() and not writer.is_closing():
            data = await asyncio.wait_for(reader.read(BUFFER_SIZE), timeout=TIMEOUT)
            if not data:
                break
            writer.write(data)
            await asyncio.wait_for(writer.drain(), timeout=TIMEOUT)
    except asyncio.TimeoutError:
        logging.error("Timeout occurred during pipe operation")
    except ConnectionResetError: pass
        #logging.error("Connection reset by peer")
    except Exception as e:
        logging.error(f"Error in pipe: {e}")
    finally:
        writer.close()

async def new_conn(local_reader: StreamReader, local_writer: StreamWriter) -> None:
    """Handle a new client connection.

    Args:
        local_reader (StreamReader): The input stream reader from the local client.
        local_writer (StreamWriter): The output stream writer to the local client.
    """
    try:
        http_data = await asyncio.wait_for(local_reader.read(BUFFER_SIZE), timeout=TIMEOUT)
        try:
            method, target = http_data.split(b"\r\n")[0].split(b" ")[0:2]
            host, port = target.split(b":")
        except:
            local_writer.close()
            return
    except asyncio.TimeoutError:
        logging.error("Timeout while reading HTTP data")
        local_writer.close()
        return
    except Exception as e:
        logging.error(f"Error parsing request: {e}")
        local_writer.close()
        return

    if method != b"CONNECT":
        local_writer.close()
        return

    local_writer.write(b'HTTP/1.1 200 OK\r\n\r\n')
    await local_writer.drain()

    try:
        remote_reader, remote_writer = await asyncio.open_connection(host.decode(), int(port))
    except Exception as e:
        logging.error(f"Error connecting to remote server: {e}")
        local_writer.close()
        return

    if port == b"443":
        await fragment_data(local_reader, remote_writer, target)

    try:
        await asyncio.gather(
            pipe(local_reader, remote_writer),
            pipe(remote_reader, local_writer)
        )
    except Exception as e:
        logging.error(f"Error in connection handling: {e}")
    finally:
        local_writer.close()

async def fragment_data(local_reader: StreamReader, remote_writer: StreamWriter,  host:str) -> None:
    """Fragment data to bypass content filtering.

    Args:
        local_reader (StreamReader): The input stream reader from the local client.
        remote_writer (StreamWriter): The output stream writer to the remote server.
        host (str): The host name of the remote server.
    """
    try:
        head = await asyncio.wait_for(local_reader.read(5), timeout=TIMEOUT)
        data = await asyncio.wait_for(local_reader.read(BUFFER_SIZE), timeout=TIMEOUT)
    except asyncio.TimeoutError:
        logging.error("Timeout while reading data for fragmentation")
        return
    except Exception as e:
        logging.error(f"Error reading data for fragmentation: {e}")
        return

    if not any(site in data for site in BLOCKED):
        logging.info(f"Host {host.decode()} passed without blocked content")
        remote_writer.write(head + data)
        await remote_writer.drain()
        return
    logging.info(f"Host {host.decode()} uses frgmentation to bypass content filtering")
    parts = []
    while data:
        part_len = random.randint(1, len(data))
        part = (
            bytes.fromhex("1603") +
            bytes([random.randint(0, 255)]) +
            int(part_len).to_bytes(2, byteorder="big") +
            data[:part_len]
        )
        parts.append(part)
        data = data[part_len:]

    remote_writer.write(b"".join(parts))
    await remote_writer.drain()

if __name__ == "__main__":
    BLOCKED: List[bytes] = []
    try:
        with open(BLOCKED_FILE, "rb") as f:
            BLOCKED = f.read().splitlines()
        logging.info(f"Loaded blocked sites from '{BLOCKED_FILE}'. Total entries: {len(BLOCKED)}")
    except FileNotFoundError:
        logging.warning(f"Blocked file '{BLOCKED_FILE}' not found. No sites will be blocked.")

    try:
        import sys
        if uvloop_available and not sys.platform.startswith("win"):
            logging.info("Using uvloop for event loop.")
            asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
        elif sys.platform.startswith("win"):
            logging.info("Using WindowsSelectorEventLoopPolicy.")
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        try:
            loop.run_until_complete(main())
        except KeyboardInterrupt:
            logging.info("Ctrl+C received. Shutting down...")
            tasks = asyncio.all_tasks(loop=loop)
            for task in tasks:
                task.cancel()
            loop.run_until_complete(asyncio.gather(*tasks, return_exceptions=True))
            loop.run_until_complete(loop.shutdown_asyncgens())
        finally:
            loop.close()
    except Exception as e:
        logging.error(f"Fatal error: {e}")
    finally:
        logging.info("Server shut down.")
