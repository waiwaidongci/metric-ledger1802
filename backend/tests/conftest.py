import os,sys,tempfile
sys.path.insert(0,os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL","sqlite:///"+tempfile.mkdtemp()+"/test.db")
os.environ.setdefault("REDIS_URL","redis://127.0.0.1:6379/15")
