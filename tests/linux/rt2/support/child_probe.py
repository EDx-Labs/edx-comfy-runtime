import json, os, sys, time
open(sys.argv[1], "w", encoding="utf-8").write(json.dumps({"pid": os.getpid(), "uid": os.getuid(), "environ": dict(os.environ)}))
time.sleep(3600)
