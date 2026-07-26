import socket


HOST = "192.168.200.200"
PORT = 48899
MESSAGE = b"WIFIKIT-214028-READ"

sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.settimeout(5)

try:
    print(f"Sending discovery probe to {HOST}:{PORT}...")
    sock.sendto(MESSAGE, (HOST, PORT))

    data, addr = sock.recvfrom(4096)

    print("\nReply received:")
    print(data.decode(errors="ignore"))

except socket.timeout:
    print("No response (timeout).")

except Exception as e:
    print("Error:", e)

finally:
    sock.close()
