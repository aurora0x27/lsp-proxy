import sys
import json
import subprocess
import socket
import threading
import logging
import queue
import select
import time
from   datetime import datetime



REQUEST_PROMPT  = "REQUEST  (Editor → LSP)"
RESPONSE_PROMPT = "RESPONSE (LSP → Editor)"



def print_help():
    print("Usage: python3 lsp-proxy.py [options] <lsp_command> [args...]")
    print("options:")
    print("--mode={pipe|socket}              Select pipe mode or socket mode, default socket")
    print("--port=<port>                     Listen on this port, only for `socket` mode")
    print("--log-file=<path/to/log_file>     Assign the log file position, default `./lsp-proxy.log`")
    print("--trace-file=<path/to/trace_file> Assign the trace result output position, default `./lsp-trace.log` on pipe mode and stdout/stderr on socket mode")
    return



# Arg parser
class SimpleArgs:
    def __init__(self, argv):
        self.mode = "socket"
        self.port = 19198
        self.log_file = "lsp-proxy.log"
        self.trace_file = "lsp-proxy.trace.log"
        self.lsp_command = []

        if argv.__contains__("--help") or argv.__contains__("-h"):
            print_help()
            sys.exit(0)

        i = 1
        while i < len(argv):
            arg = argv[i]
            if arg == "--mode":
                self.mode = argv[i + 1]
                i += 2
            elif arg.startswith("--mode="):
                self.mode = argv[i][len("--mode="):]
                i += 1
            elif arg == "--port":
                self.port = int(argv[i + 1])
                i += 2
            elif arg.startswith("--port="):
                self.port = argv[i][len("--port="):]
                i += 1
            elif arg == "--trace-file":
                self.trace_file = argv[i + 1]
                i += 2
            elif arg.startswith("--trace-file="):
                self.trace_file = argv[i][len("--trace-file="):]
                i += 1
            elif arg == "--log-file":
                self.log_file = argv[i + 1]
                i += 2
            elif arg.startswith("--log-file="):
                self.log_file = argv[i][len("--log-file="):]
                i += 1
            elif arg.startswith("--"):
                print(f"Unknown option: {arg}", file=sys.stderr)
                sys.exit(1)
            elif arg == "-":
                self.lsp_command = argv[i + 1:]
                break
            else:
                # Else is all regarded as lsp command
                self.lsp_command = argv[i:]
                break

        if not self.lsp_command:
            print_help()
            sys.exit(1)



class MultiLogger:
    def __init__(self, args):
        try:
            self.log_file_handler = logging.FileHandler(args.log_file, mode="a", encoding="utf-8")
            self.trace_file_handler = logging.FileHandler(args.trace_file, mode="a", encoding="utf-8")
        except Exception as e:
            sys.stderr.write(f"Failed to open trace output: {e}\n")

        self.logger = logging.getLogger("lsp-proxy")
        self.tracer = logging.getLogger("Trace")

        self.logger.setLevel(logging.INFO)
        self.tracer.setLevel(logging.DEBUG)

        self.logger.addHandler(self.log_file_handler)
        self.tracer.addHandler(self.trace_file_handler)
        
        self.lock = threading.Lock()
        pass

    def log(self, msg):
        with self.lock:
            self.logger.info(msg)
        pass

    def trace(self, msg):
        with self.lock:
            self.tracer.debug(msg)
        pass

    def close(self):
        for handler in self.logger.handlers:
            handler.flush()
            handler.close()
        self.logger.handlers.clear()

        for handler in self.tracer.handlers:
            handler.flush()
            handler.close()
        self.tracer.handlers.clear()




# Main service
class LSPProxy:
    def __init__(self, args):
        self.args = args
        # will delete
        self.log_file = open(args.log_file, "a", encoding="utf-8") if args.log_file else None
        self.lsp_process = None
        self.logger = MultiLogger(args)

    def format_trace(self, prompt, data):
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")
        try:
            parsed = json.loads(data)
            formatted = json.dumps(parsed, indent=4, ensure_ascii=False, sort_keys=False)
        except Exception:
            self.logger.log(f"Not an available json: \n```\n{data}\n```\n")
            formatted = data

        return f"\n[{timestamp}] {prompt}:\n{formatted}\n"

    def format_log(self, msg):

        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")
        return f"[{timestamp}]: {msg}"

    def pipe_mode(self):
        self.lsp_process = subprocess.Popen(
            self.args.lsp_command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=False,
            bufsize=-1,
        )

        stdin_queue  = queue.Queue()
        stdout_queue = queue.Queue()

        time.sleep(0.05)

        if self.lsp_process is None or self.lsp_process.stdin is None:
            raise RuntimeError("LSP process or its file descriptors is not available")

        # Collect the request message from editor to lsp
        def read_stdin(self):
            while True:
                if self.lsp_process.poll() is not None:
                    self.logger.log(self.format_log("LSP Process exits"))
                    break

                rlist, _, _ = select.select([sys.stdin], [], [], 1)
                if rlist:
                    header = b""
                    while not header.endswith(b"\r\n\r\n"):
                        chunk = sys.stdin.buffer.read(1)
                        if not chunk:
                            return  # EOF
                        header += chunk
                    content_length = 0
                    for line in header.split(b"\r\n"):
                        if line.startswith(b"Content-Length:"):
                            content_length = int(line.split(b":")[1].strip())
                            break
                    body = sys.stdin.buffer.read(content_length) if content_length > 0 else b""
                    stdin_queue.put((header, body))
                    self.logger.log("Put message into stdin_queue")

        # Collect the message from lsp to editor
        def read_stdout(self):
            while True:
                if self.lsp_process.poll() is not None:
                    self.logger.log(self.format_log("LSP Process exits"))
                    break

                rlist, _, _ = select.select([self.lsp_process.stdout], [], [], 1)
                if rlist:
                    header = b""
                    while not header.endswith(b"\r\n\r\n"):
                        chunk = self.lsp_process.stdout.read(1)
                        if not chunk:
                            break  # EOF
                        header += chunk

                    self.logger.log(self.format_log(f"LSP sent header: `{header}`"))
                    content_length = 0
                    for line in header.split(b"\r\n"):
                        if line.startswith(b"Content-Length"):
                            content_length = int(line.split(b":")[1].strip())
                            break
                    if content_length == 0:
                        self.logger.log(self.format_log("LSP sent empty message"))
                        continue
                    body = b""
                    remaining = content_length
                    while remaining > 0:
                        chunk = self.lsp_process.stdout.read(remaining)
                        if not chunk:
                            self.logger.log(self.format_log("LSP connection closed prematurely"))
                            break
                        body += chunk
                        remaining -= len(chunk)

                    if not body:
                        self.logger.log(self.format_log("Reveived empty body from lsp, skipping..."))
                        continue
                    stdout_queue.put((header, body))
                    self.logger.log("Put message into stdout_queue")

        stdin_thread = threading.Thread(target=read_stdin, args=(self,), daemon=True)
        stdin_thread.start()
        self.logger.log(self.format_log(f"Starting to forward stdin of lsp process: {self.lsp_process.pid}..."))

        stdout_thread = threading.Thread(target=read_stdout, args=(self,), daemon=True)
        self.logger.log(self.format_log(f"Starting to forward stdout of lsp process: {self.lsp_process.pid}..."))
        stdout_thread.start()

        # Main loop: forward messages
        while True:
            try:
                if not stdin_queue.empty():
                    header, body = stdin_queue.get(timeout=0.1)
                    self.lsp_process.stdin.write(header + body)
                    self.lsp_process.stdin.flush()
                    self.logger.trace(self.format_trace(REQUEST_PROMPT, body))
                    self.logger.log("Forward stdin message")
                    # self.logger.log(f"Sending to LSP:\n{header + body}")
                if not stdout_queue.empty():
                    header, body = stdout_queue.get(timeout=0.1)
                    sys.stdout.buffer.write(header + body)
                    sys.stdout.buffer.flush()
                    self.logger.trace(self.format_trace(RESPONSE_PROMPT, body))
                    self.logger.log(f"Sending to Editor:\n{header + body}")
                    # self.logger.log("Forward stdout message")
            except queue.Empty:
                # No data in queue
                pass

    # Socket Mode:
    # Editor communicate with language server by socket
    def socket_mode(self):
        # Start lsp process
        self.lsp_process = subprocess.Popen(
            self.args.lsp_command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=False,
            bufsize=-1,
        )
        
        if self.lsp_process is None or self.lsp_process.stdin is None:
            raise RuntimeError("LSP process or its file decriptors is not available")

        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind(("127.0.0.1", self.args.port))
        server.listen(5)
        self.logger.log(self.format_log(f"LSP Proxy listening on port {self.args.port}"))

        # socket_queue store message to forward from editor
        socket_queue = queue.Queue()
        # stdout_queue store message to forward from lsp
        stdout_queue = queue.Queue()

        def read_socket(self, sock):
            try:
                while True:
                    if self.lsp_process.poll() is not None:
                        self.logger.log(self.format_log("LSP Process exits"))
                        break
                    rlist, _, _ = select.select([sock], [], [], 1)
                    if rlist:
                        header = b""
                        while not header.endswith(b"\r\n\r\n"):
                            chunk = sock.recv(1)
                            if not chunk:
                                break
                            header += chunk
                        # self.logger.log(self.format_log(f"Editor sent header: `{header}`"))
                        content_length = 0
                        for line in header.split(b"\r\n"):
                            if line.startswith(b"Content-Length"):
                                content_length = int(line.split(b":")[1].strip())
                                break
                        if content_length == 0:
                            # self.logger.log(self.format_log("Editor sent empty message"))
                            continue
                        body = sock.recv(content_length) if content_length > 0 else b""
                        socket_queue.put((header, body, sock))
                        # self.logger.log(self.format_log("Put message into socket queue"))
            except (ConnectionResetError, BrokenPipeError) as e:
                self.logger.log(self.format_log(f"Client socket closed: {e}"))
            except Exception as e:
                self.logger.log(self.format_log(f"Unexpected error in read_socket: {e}"))
            finally:
                sock.close()
                self.logger.log(self.format_log(f"Socket {sock.fileno()} closed"))

        def read_stdout(self, sock):
            while True:
                if self.lsp_process.poll() is not None:
                    self.logger.log(self.format_log("LSP Process exits"))
                    break

                rlist, _, _ = select.select([self.lsp_process.stdout], [], [], 1)
                if rlist:
                    header = b""
                    while not header.endswith(b"\r\n\r\n"):
                        chunk = self.lsp_process.stdout.read(1)
                        if not chunk:
                            break  # EOF
                        header += chunk

                    # self.logger.log(self.format_log(f"LSP sent header: `{header}`"))
                    content_length = 0
                    for line in header.split(b"\r\n"):
                        if line.startswith(b"Content-Length"):
                            content_length = int(line.split(b":")[1].strip())
                            break;
                    if content_length == 0:
                        self.logger.log(self.format_log("LSP sent empty message"))
                        continue
                    body = b""
                    remaining = content_length
                    while remaining > 0:
                        chunk = self.lsp_process.stdout.read(remaining)
                        if not chunk:
                            self.logger.log(self.format_log("LSP connection closed prematurely"))
                            break
                        body += chunk
                        remaining -= len(chunk)

                    if not body:
                        self.logger.log(self.format_log("Reveived empty body from lsp, skipping..."))
                        continue
                    stdout_queue.put((header, body, sock))
                    # self.logger.log("Put message into stdout_queue")

        def read_stderr(self):
            try:
                while True:
                    if self.lsp_process.poll() is not None:
                        self.logger.log(self.format_log("[stderr] LSP process has exited, stopping stderr reader..."))
                        break
                    line = self.lsp_process.stderr.readline()
                    if not line:
                        break
                    self.logger.log(self.format_log(f"[LSP stderr] {str(line.decode("utf-8", errors="replace").strip())}"))
            except Exception as e:
                self.logger.log(self.format_log(f"[stderr] Exception: {e}"))

        def forward_message(self):
            while True:
                try:
                    if not socket_queue.empty():
                        msg, body, _ = socket_queue.get(timeout=0.1)
                        self.lsp_process.stdin.write(msg + body)
                        self.lsp_process.stdin.flush()
                        self.logger.trace(self.format_trace(REQUEST_PROMPT, body))
                        # self.logger.log("Forward stdin message")
                    if not stdout_queue.empty():
                        msg, body, sock = stdout_queue.get(timeout=0.1)
                        sock.send(msg + body)
                        self.logger.trace(self.format_trace(RESPONSE_PROMPT, body))
                        # self.logger.log("Forward socket message")

                except queue.Empty:
                    continue
                except OSError:
                    self.logger.log(self.format_log("Cannot connect to socket, process exits"))
                    break

        stderr_thread = threading.Thread(target=read_stderr, args=(self,), daemon=True)
        stderr_thread.start()
        time.sleep(0.05)
        if self.lsp_process.poll() is not None:
            self.logger.log(self.format_log("LSP process exited"))
            return

        self.logger.log(self.format_log(f"Starting to collect message from stdout of lsp process: {self.lsp_process.pid}..."))

        forward_thread = threading.Thread(target=forward_message, args=(self,), daemon=True)
        forward_thread.start()
        self.logger.log(self.format_log(f"Starting to forward messages"))

        client_sock, addr = server.accept()
        self.logger.log(self.format_log(f"New connection from socket {client_sock.fileno()}, at address: {addr}"))
        sock_thread = threading.Thread(target=read_socket, args=(self, client_sock,), daemon=True)
        sock_thread.start()
        stdout_thread = threading.Thread(target=read_stdout, args=(self, client_sock,), daemon=True)
        stdout_thread.start()

        while True:
            if self.lsp_process.poll() is not None:
                self.logger.log(self.format_log("LSP process exited"))
                break
            if not forward_thread.is_alive() or not sock_thread.is_alive() or not stdout_thread.is_alive() or not stderr_thread.is_alive():
                self.logger.log(self.format_log("Some threads exited"))
                break
            time.sleep(0.05)

    def run(self):
        try:
            if self.args.mode == "pipe":
                self.pipe_mode()
            elif self.args.mode == "socket":
                self.socket_mode()
            else:
                print(f"Unknown mode {self.args.mode}")
                exit(1)
        except KeyboardInterrupt:
            self.logger.log(self.format_log("Shutting down..."))
        finally:
            if self.lsp_process:
                self.lsp_process.terminate()
            if self.log_file:
                self.log_file.close()



def main():
    args = SimpleArgs(sys.argv)
    service = LSPProxy(args)
    service.run()



if __name__ == "__main__":
    main()
