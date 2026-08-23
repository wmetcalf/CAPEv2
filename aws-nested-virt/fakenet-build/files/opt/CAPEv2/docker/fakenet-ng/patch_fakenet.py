"""Patch FakeNet-NG for DivertTraffic=No (listen-only) mode.

Fixes three upstream bugs:
1. fn_addr is uninitialized when DivertTraffic=No
2. DiverterListenerCallbacks crashes when diverter is None
3. ThreadedHTTPServer is not actually threaded (missing ThreadingMixIn),
   causing TLS handshakes to block the accept loop and starve connections
"""
import importlib.util
import os

pkg_dir = os.path.dirname(importlib.util.find_spec("fakenet").origin)

# --- Patch 1: fn_addr uninitialized in fakenet.py ---
fakenet_py = os.path.join(pkg_dir, "fakenet.py")
with open(fakenet_py, "r") as f:
    source = f.read()

old = "    def start(self):\n"
new = "    def start(self):\n        fn_addr = '0.0.0.0'\n"
if new not in source:
    source = source.replace(old, new, 1)
    with open(fakenet_py, "w") as f:
        f.write(source)
    print(f"Patched {fakenet_py}: fn_addr initialization")

# --- Patch 2: DiverterListenerCallbacks with None diverter ---
diverterbase_py = os.path.join(pkg_dir, "diverters", "diverterbase.py")
with open(diverterbase_py, "r") as f:
    source = f.read()

# Make isProcessBlackListed safe when diverter is None
old_method = '''\
    def isProcessBlackListed(self, proto, sport):
        """Check if the process is blacklisted.
        """
        return self.__diverter.isProcessBlackListed(proto, sport=sport)'''

new_method = '''\
    def isProcessBlackListed(self, proto, sport):
        """Check if the process is blacklisted.
        """
        if self.__diverter is None:
            return False, None, None
        return self.__diverter.isProcessBlackListed(proto, sport=sport)'''

if "if self.__diverter is None:" not in source:
    source = source.replace(old_method, new_method)

# Make logNbi safe when diverter is None
old_log = '''\
        self.__diverter.logNbi(sport, nbi, proto, application_layer_proto,
                               is_ssl_encrypted)'''

new_log = '''\
        if self.__diverter is None:
            return
        self.__diverter.logNbi(sport, nbi, proto, application_layer_proto,
                               is_ssl_encrypted)'''

if 'if self.__diverter is None:\n            return\n        self.__diverter.logNbi' not in source:
    source = source.replace(old_log, new_log)

with open(diverterbase_py, "w") as f:
    f.write(source)
print(f"Patched {diverterbase_py}: None-safe callbacks")

# --- Patch 3: Make ThreadedHTTPServer actually threaded ---
http_listener_py = os.path.join(pkg_dir, "listeners", "HTTPListener.py")
with open(http_listener_py, "r") as f:
    source = f.read()

# Add ThreadingMixIn so each connection is handled in its own thread.
# Without this, a slow/stuck TLS handshake blocks all subsequent connections.
old_class = "class ThreadedHTTPServer(http.server.HTTPServer):"
new_class = "class ThreadedHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):\n    daemon_threads = True"

if "ThreadingMixIn" not in source:
    # Ensure socketserver is imported
    if "import socketserver" not in source:
        source = source.replace("import threading\n", "import threading\nimport socketserver\n", 1)
    source = source.replace(old_class, new_class)
    with open(http_listener_py, "w") as f:
        f.write(source)
    print(f"Patched {http_listener_py}: ThreadingMixIn for concurrent connections")
