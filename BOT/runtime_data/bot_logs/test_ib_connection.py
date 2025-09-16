from ib_insync import IB
ib = IB()
ib.RequestTimeout = 60  # נגד timeouts קצרים
print("Connecting...")
ib.connect('127.0.0.1', 7497, clientId=1001)
print("connected:", ib.isConnected())
if ib.isConnected():
    print("Server time:", ib.reqCurrentTime())
ib.disconnect()
