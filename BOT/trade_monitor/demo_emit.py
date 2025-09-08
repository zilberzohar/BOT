# Optional: quick demo emitter to test the dashboard without your bot.
import random, time
from monitor import info, warn
symbols = ["TSLA", "AAPL", "SPY"]

i = 0
while True:
    sym = random.choice(symbols)
    px = 100 + random.random()*50
    info("DATA", symbol=sym, price=px, details={"bar_time":"now"})
    if i % 20 == 0:
        info("SIGNAL", symbol=sym, side=random.choice(["Long","Short"]), price=px, details={"logic":"DEMO"})
    if i % 37 == 0:
        warn("BLOCK", symbol=sym, reason=random.choice(["VWAP check failed","Direction filter (Long Only)","Insufficient cash"]))
    time.sleep(0.2)
    i += 1
