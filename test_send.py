"""
test_send.py
────────────
Sends a sample ESC/POS receipt to the emulator via TCP.
Usage:  python test_send.py [host] [port]
Default: 127.0.0.1:9100
"""

import socket
import sys
import time

HOST = sys.argv[1] if len(sys.argv) > 1 else "127.0.0.1"
PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 9100

ESC = b'\x1b'
GS  = b'\x1d'
LF  = b'\x0a'

def esc_init():       return ESC + b'@'
def esc_align(n):     return ESC + b'a' + bytes([n])   # 0=L 1=C 2=R
def esc_bold(on):     return ESC + b'E' + bytes([1 if on else 0])
def esc_double_h(on): return GS  + b'!' + bytes([0x10 if on else 0x00])
def esc_underline(n): return ESC + b'-' + bytes([n])
def esc_feed(n):      return ESC + b'd' + bytes([n])
def esc_cut():        return GS  + b'V' + b'\x00'
def text(s):          return s.encode('utf-8') + LF


receipt = b''.join([
    esc_init(),

    # Header
    esc_align(1),
    esc_double_h(True),
    esc_bold(True),
    text("SUPER MART"),
    esc_double_h(False),
    esc_bold(False),
    text("123 Colon Street, Cebu City"),
    text("Tel: (032) 555-0198"),
    text("VAT Reg TIN: 123-456-789-000"),
    esc_feed(1),

    # Separator
    text("-" * 42),

    # Items
    esc_align(0),
    esc_bold(True),
    text(f"{'ITEM':<28}{'QTY':>4}{'AMOUNT':>10}"),
    esc_bold(False),
    text("-" * 42),

    text(f"{'Jasmine Rice 5kg':<28}{'1':>4}{'245.00':>10}"),
    text(f"{'Cooking Oil 1L':<28}{'2':>4}{'178.00':>10}"),
    text(f"{'Instant Noodles x10':<28}{'3':>4}{'135.00':>10}"),
    text(f"{'Canned Sardines 155g':<28}{'4':>4}{'92.00':>10}"),
    text(f"{'Toothpaste 150ml':<28}{'1':>4}{'89.50':>10}"),
    text(f"{'Shampoo 180ml':<28}{'1':>4}{'112.75':>10}"),
    text("-" * 42),

    # Totals
    esc_align(2),
    text(f"SUBTOTAL:          PHP  852.25"),
    text(f"VAT (12%):         PHP  102.27"),
    esc_bold(True),
    text(f"TOTAL:             PHP  954.52"),
    esc_bold(False),
    esc_feed(1),

    # Payment
    esc_align(0),
    text("-" * 42),
    esc_align(2),
    text(f"CASH:              PHP 1,000.00"),
    text(f"CHANGE:            PHP   45.48"),
    esc_feed(1),

    # Footer
    esc_align(1),
    text("-" * 42),
    esc_underline(1),
    text("Thank you for shopping with us!"),
    esc_underline(0),
    text("Please come again  😊"),
    esc_feed(1),
    text("Cashier: Maria Santos"),
    text("Terminal: POS-03"),
    text("Trans#: 00045892"),

    esc_feed(3),
    esc_cut(),
])

print(f"Connecting to {HOST}:{PORT} ...")
try:
    with socket.create_connection((HOST, PORT), timeout=5) as s:
        s.sendall(receipt)
        print(f"Sent {len(receipt)} bytes of ESC/POS receipt.")
    print("Done.")
except ConnectionRefusedError:
    print(f"Error: Connection refused. Is the printer emulator running on {HOST}:{PORT}?")
    sys.exit(1)
except socket.timeout:
    print(f"Error: Connection timed out trying to reach {HOST}:{PORT}.")
    sys.exit(1)
except Exception as e:
    print(f"Error: An unexpected error occurred: {e}")
    sys.exit(1)

