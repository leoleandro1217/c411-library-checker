import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import integrations
import server


class DummyResponse:
    status = 200
    def __init__(self, data):
        self.body = io.BytesIO(data)
    def __enter__(self): return self
    def __exit__(self, *_): pass
    def read(self, n=-1): return self.body.read(n)


class IntegrationTests(unittest.TestCase):
    def test_relative_path_requires_explicit_prefix(self):
        self.assertEqual(integrations.jellyfin_relative_path('/data/Films/Film.mkv', '/data'), 'Films/Film.mkv')
        self.assertIsNone(integrations.jellyfin_relative_path('/other/Film.mkv', '/data'))
        self.assertIsNone(integrations.jellyfin_relative_path('/data2/Film.mkv', '/data'))
        self.assertIsNone(integrations.jellyfin_relative_path('/data/Film.mkv', ''))

    def test_invalid_integration_url(self):
        for url in ('file:///etc/passwd', 'http://user:password@nas:8080', 'http://nas/path', 'http://nas?q=secret'):
            with self.assertRaises(ValueError):
                integrations.base_url(url)

    def test_jellyfin_pagination_and_header(self):
        def reply(req, timeout):
            self.assertEqual(req.get_header('X-emby-token'), 'jf-secret')
            query = req.full_url
            if 'StartIndex=0' in query:
                return DummyResponse(json.dumps({'Items':[{'Path':'/videos/Film.mkv','Id':'abc','Name':'Film'}], 'TotalRecordCount':2}).encode())
            return DummyResponse(json.dumps({'Items':[{'Path':'/videos/Film2.mkv','Id':'def','Name':'Film 2'}], 'TotalRecordCount':2}).encode())
        with patch.object(integrations,'JELLYFIN_URL','http://jellyfin:8096'), patch.object(integrations,'JELLYFIN_API_KEY','jf-secret'), patch('integrations.urlopen',side_effect=reply):
            movies=integrations.jellyfin_movies()
            self.assertEqual(len(movies),2)

    def test_qbittorrent_bearer_redacts_sensitive(self):
        torrent={'name':'Test','hash':'a'*40,'tracker':'https://tracker/private-passkey','save_path':'/secret','state':'stalledUP','upspeed':200,'ratio':2.3}
        with patch.object(integrations,'QBIT_URL','http://qbittorrent:8080'), patch.object(integrations,'QBIT_API_KEY','qbt_FAKE'), patch('integrations.urlopen',return_value=DummyResponse(json.dumps([torrent]).encode())) as mocked:
            result=integrations.qbittorrent_torrents()
            self.assertEqual(result[0]['name'],'Test')
            self.assertNotIn('tracker', result[0])
            self.assertNotIn('save_path',result[0])
            self.assertEqual(mocked.call_args.args[0].get_header('Authorization'),'Bearer qbt_FAKE')

    def test_cross_seed_requires_key(self):
        with patch.object(integrations,'CROSS_SEED_URL','http://cross-seed:2468'), patch.object(integrations,'CROSS_SEED_API_KEY',''):
            with self.assertRaises(ValueError): integrations.cross_seed_trigger_search()

    def test_cross_seed_search_confirmed_action(self):
        with patch.object(integrations,'CROSS_SEED_URL','http://cross-seed:2468'), patch.object(integrations,'CROSS_SEED_API_KEY','secret'), patch('integrations.urlopen',return_value=DummyResponse(b'')) as mocked:
            self.assertTrue(integrations.cross_seed_trigger_search())
            req=mocked.call_args.args[0]
            self.assertEqual(req.get_header('X-api-key'),'secret')
            self.assertEqual(json.loads(req.data), {'name':'search'})

    def test_jellyfin_sync_safe_media_mapping(self):
        with tempfile.TemporaryDirectory() as directory:
            base=Path(directory)
            with patch.object(server,'DATA_DIR',base), patch.object(integrations,'JELLYFIN_PATH_PREFIX','/jf'), patch('integrations.jellyfin_movies',return_value=[
                    {'Path':'/jf/Film.mkv','Id':'a1','Name':'Le Film','OriginalTitle':'The Film','ProductionYear':2011,'ProviderIds':{'Imdb':'tt1'}},
                    {'Path':'/other/NotOurFilm.mkv','Id':'b2','Name':'Not our film'},
            ]):
                server.initialize()
                with server.db() as conn:
                    conn.execute('''INSERT INTO movies (path,title,size,mtime,status) VALUES (?,?,?,?,?)''',('Film.mkv','Film',100,0,'found'))
                total, linked=server.sync_jellyfin()
                self.assertEqual((total,linked),(2,1))
                with server.db() as conn:
                    row=conn.execute('SELECT title,year,imdb FROM jellyfin_movies').fetchone()
                    self.assertEqual((row['title'],row['year'],row['imdb']),('Le Film',2011,'tt1'))
                    self.assertEqual(conn.execute('SELECT status FROM movies WHERE path="Film.mkv"').fetchone()[0],'pending')

    def test_candidate_shortlist(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(server,'DATA_DIR',Path(directory)), patch('server.search_c411',return_value=['Film.2001.BluRay','Film.2003.WEB-DL']):
                server.initialize()
                with server.db() as conn:
                    status,_,_=server.classify(conn,'Film','',2001,'Film.2001.mkv')
                    self.assertEqual(status,'found')
                    self.assertEqual(conn.execute('SELECT COUNT(*) FROM candidates').fetchone()[0],2)

if __name__=='__main__': unittest.main()
