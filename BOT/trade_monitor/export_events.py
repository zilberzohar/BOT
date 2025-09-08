import pandas as pd
from sqlalchemy import create_engine
from monitor_settings import SQLITE_PATH
from datetime import datetime

def export_csv():
    engine = create_engine(f"sqlite:///{SQLITE_PATH}")
    df = pd.read_sql("SELECT * FROM events ORDER BY ts", engine)
    stamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    out = f"runtime_data/events_session_{stamp}.csv"
    df.to_csv(out, index=False)
    print(f"Saved {out}")

if __name__ == "__main__":
    export_csv()
