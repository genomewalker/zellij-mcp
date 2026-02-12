#!/usr/bin/env python3
"""
Zellij Terminal Proxy - Provides terminal attachment for background sessions.

Uses `script` command to create a pseudo-terminal that attaches to Zellij,
enabling pane reading operations in background sessions.

Usage:
    # Start proxy for a session
    python zellij-proxy.py --session zellij-agent --socket /tmp/zellij-proxy.sock

    # Query from another process
    echo '{"cmd":"read","pane_id":42}' | nc -U /tmp/zellij-proxy.sock
"""

import argparse
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import tempfile
from pathlib import Path


class ZellijProxy:
    """Proxy server that maintains terminal attachment to a Zellij session."""

    def __init__(self, session: str, socket_path: str):
        self.session = session
        self.socket_path = socket_path
        self.script_proc = None
        self.running = False
        self.lock = threading.Lock()
        self.plugin_path = os.path.expanduser(
            "~/.local/share/zellij-mcp/plugins/zellij-pane-bridge.wasm"
        )

    def start(self):
        """Start the proxy server."""
        # Start `script` with zellij attach to create a pseudo-terminal
        script_log = tempfile.mktemp(suffix='.log')
        self.script_proc = subprocess.Popen(
            ['script', '-q', '-c', f'zellij attach {self.session}', script_log],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True
        )

        # Give Zellij time to attach
        time.sleep(2)

        if self.script_proc.poll() is not None:
            print(f"Failed to attach to session {self.session}", file=sys.stderr)
            return

        self.running = True
        print(f"Zellij proxy attached to session: {self.session}", file=sys.stderr)

        # Start socket server
        self._run_server()

    def _run_server(self):
        """Run the Unix socket server."""
        # Clean up old socket
        if os.path.exists(self.socket_path):
            os.unlink(self.socket_path)

        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(self.socket_path)
        server.listen(5)
        server.settimeout(1.0)

        print(f"Zellij proxy listening on {self.socket_path}", file=sys.stderr)

        try:
            while self.running:
                try:
                    conn, _ = server.accept()
                    threading.Thread(target=self._handle_client, args=(conn,), daemon=True).start()
                except socket.timeout:
                    # Check if script process is still alive
                    if self.script_proc and self.script_proc.poll() is not None:
                        print("Zellij attachment lost, stopping proxy", file=sys.stderr)
                        self.running = False
                    continue
        finally:
            server.close()
            if os.path.exists(self.socket_path):
                os.unlink(self.socket_path)
            self.stop()

    def _handle_client(self, conn):
        """Handle a client connection."""
        try:
            data = conn.recv(4096).decode('utf-8').strip()
            if not data:
                return

            request = json.loads(data)
            cmd = request.get('cmd', '')

            if cmd == 'read':
                pane_id = request.get('pane_id')
                response = self._read_pane(pane_id)
            elif cmd == 'focus':
                pane_id = request.get('pane_id')
                response = self._focus_pane(pane_id)
            elif cmd == 'dump':
                response = self._dump_screen()
            elif cmd == 'list':
                response = self._list_panes()
            elif cmd == 'status':
                attached = self.script_proc and self.script_proc.poll() is None
                response = {'success': True, 'session': self.session, 'attached': attached}
            elif cmd == 'stop':
                self.running = False
                response = {'success': True, 'message': 'Stopping proxy'}
            else:
                response = {'success': False, 'error': f'Unknown command: {cmd}'}

            conn.send(json.dumps(response).encode('utf-8'))
        except Exception as e:
            try:
                conn.send(json.dumps({'success': False, 'error': str(e)}).encode('utf-8'))
            except:
                pass
        finally:
            conn.close()

    def _plugin_cmd(self, cmd: str, payload: dict = None) -> dict:
        """Execute a plugin command."""
        if not os.path.exists(self.plugin_path):
            return {'success': False, 'error': 'Plugin not found'}

        payload_json = json.dumps(payload) if payload else '{}'
        try:
            result = subprocess.run(
                ['timeout', '5', 'zellij', '-s', self.session, 'pipe',
                 '-p', f'file://{self.plugin_path}', '-n', cmd, '--', payload_json],
                capture_output=True, text=True, timeout=7
            )
            stdout = result.stdout.strip()
            if stdout:
                # Handle duplicate JSON responses
                if '}{' in stdout:
                    stdout = stdout.split('}{')[0] + '}'
                return json.loads(stdout)
            return {'success': False, 'error': 'No output'}
        except json.JSONDecodeError as e:
            return {'success': False, 'error': f'JSON decode error: {e}', 'raw': stdout}
        except Exception as e:
            return {'success': False, 'error': str(e)}

    def _list_panes(self) -> dict:
        """List all panes via plugin."""
        return self._plugin_cmd('list')

    def _focus_pane(self, pane_id: int) -> dict:
        """Focus a pane by ID using the plugin."""
        return self._plugin_cmd('focus', {'pane_id': pane_id})

    def _dump_screen(self) -> dict:
        """Dump the current screen content by typing command into attached session."""
        with self.lock:
            try:
                # Create temp file path
                tmp_path = tempfile.mktemp(suffix='.txt')

                # Remove old file if exists
                try:
                    os.unlink(tmp_path)
                except:
                    pass

                # Send dump-screen command through the script's stdin
                # This types the command INTO the attached zellij session
                if self.script_proc and self.script_proc.stdin:
                    cmd = f"zellij action dump-screen {tmp_path}\n"
                    self.script_proc.stdin.write(cmd.encode())
                    self.script_proc.stdin.flush()

                    # Wait for file to be created
                    for _ in range(10):
                        time.sleep(0.2)
                        if os.path.exists(tmp_path) and os.path.getsize(tmp_path) > 0:
                            break

                    # Read the output
                    try:
                        with open(tmp_path, 'r') as f:
                            content = f.read()
                        os.unlink(tmp_path)
                        return {'success': True, 'content': content}
                    except FileNotFoundError:
                        return {'success': True, 'content': ''}
                else:
                    return {'success': False, 'error': 'Script process not available'}
            except Exception as e:
                return {'success': False, 'error': str(e)}

    def _read_pane(self, pane_id: int) -> dict:
        """Read content from a specific pane."""
        # Focus the pane via plugin
        focus_result = self._focus_pane(pane_id)
        if not focus_result.get('success'):
            return focus_result

        # Small delay for focus to take effect
        time.sleep(0.3)

        # Dump screen content
        return self._dump_screen()

    def stop(self):
        """Stop the proxy server."""
        self.running = False
        if self.script_proc:
            try:
                self.script_proc.terminate()
                self.script_proc.wait(timeout=2)
            except:
                try:
                    self.script_proc.kill()
                except:
                    pass


def client_request(socket_path: str, request: dict) -> dict:
    """Send a request to the proxy server."""
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.settimeout(10)
        sock.connect(socket_path)
        sock.send(json.dumps(request).encode('utf-8'))
        response = sock.recv(65536).decode('utf-8')
        return json.loads(response)
    finally:
        sock.close()


def main():
    parser = argparse.ArgumentParser(description='Zellij Terminal Proxy')
    parser.add_argument('--session', '-s', help='Zellij session name (required for server mode)')
    parser.add_argument('--socket', default='/tmp/zellij-proxy.sock', help='Unix socket path')
    parser.add_argument('--client', action='store_true', help='Run as client (send command)')
    parser.add_argument('--cmd', help='Command for client mode (read, focus, dump, list, status, stop)')
    parser.add_argument('--pane-id', type=int, help='Pane ID for read/focus commands')

    args = parser.parse_args()

    if args.client:
        # Client mode - send command to existing proxy
        request = {'cmd': args.cmd or 'status'}
        if args.pane_id:
            request['pane_id'] = args.pane_id
        try:
            result = client_request(args.socket, request)
            print(json.dumps(result, indent=2))
        except FileNotFoundError:
            print(json.dumps({'success': False, 'error': f'Proxy not running (socket not found: {args.socket})'}))
            sys.exit(1)
        except Exception as e:
            print(json.dumps({'success': False, 'error': str(e)}))
            sys.exit(1)
    else:
        # Server mode - start proxy
        if not args.session:
            parser.error("--session is required for server mode")
        proxy = ZellijProxy(args.session, args.socket)

        def signal_handler(sig, frame):
            print("\nStopping proxy...", file=sys.stderr)
            proxy.stop()
            sys.exit(0)

        signal.signal(signal.SIGINT, signal_handler)
        signal.signal(signal.SIGTERM, signal_handler)

        proxy.start()


if __name__ == '__main__':
    main()
