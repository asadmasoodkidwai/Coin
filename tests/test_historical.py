import json,sqlite3,threading
import numpy as np
import pytest
from app.learn import Learner,FEATS

def test_model_roundtrip_and_schema(tmp_path):
    L=Learner(sqlite3.connect(':memory:'),threading.RLock())
    L.W=np.zeros((len(FEATS)+1,3));L.meta=dict(n_obs=50,historical=True)
    p=tmp_path/'model.json';L.save_model(p)
    M=Learner(sqlite3.connect(':memory:'),threading.RLock());M.load_model(p)
    assert M._ready() and np.array_equal(L.W,M.W)
    d=json.loads(p.read_text());d['features'].reverse();p.write_text(json.dumps(d))
    with pytest.raises(ValueError): M.load_model(p)

def test_unverified_local_predictions_not_training_labels():
    L=Learner(sqlite3.connect(':memory:'),threading.RLock())
    with L.db:
        L.db.execute('insert into observations(ts,feats,action,source) values(?,?,?,?)',(1,json.dumps([0.]*13),'buy','student'))
    assert len(L._data()[1])==0
    with L.db: L.db.execute('update observations set fwd_ret=-.04')
    assert L._data()[1].tolist()==[2] # no holding: ignore, cannot sell
