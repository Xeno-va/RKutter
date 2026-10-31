"""Open the raw network connection to the UV board and hand it to the program that asked.

Sending raw network frames needs a permission that is given once to a separate copy of
Python (.venv/bin/python-laser, made by install.sh). A program holding that permission cannot read the desktop's
settings, so the laser panel itself runs on plain Python and starts this helper only to open
the connection: the helper opens it, passes it back over the pipe it was given, and exits.

usage: python-laser rawsock.py <interface> <ethertype> <pipe fd>
"""
import socket
import sys


def main():
    iface, ethertype, fd = sys.argv[1], int(sys.argv[2], 0), int(sys.argv[3])
    raw = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(ethertype))
    raw.bind((iface, 0))
    with socket.socket(fileno=fd) as pipe:
        socket.send_fds(pipe, [b"ok"], [raw.fileno()])


if __name__ == "__main__":
    main()
