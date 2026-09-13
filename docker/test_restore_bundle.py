"""Archive regressions: no Docker or personal volumes required."""
import importlib.util
import io
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch

spec=importlib.util.spec_from_file_location('restore_bundle',Path(__file__).with_name('restore-bundle.py'))
restore=importlib.util.module_from_spec(spec);spec.loader.exec_module(restore)

def archive(entries):
    stream=io.BytesIO()
    with tarfile.open(fileobj=stream,mode='w:gz') as tar:
        for name,kind,value in entries:
            info=tarfile.TarInfo(name)
            if kind=='file': info.size=len(value);tar.addfile(info,io.BytesIO(value))
            else:
                info.type=tarfile.SYMTYPE if kind=='sym' else tarfile.LNKTYPE if kind=='hard' else tarfile.CHRTYPE
                info.linkname=value;tar.addfile(info)
    stream.seek(0);return stream

class RestoreValidationTests(unittest.TestCase):
    def validate(self,entries):
        with tarfile.open(fileobj=archive(entries),mode='r:gz') as tar:return restore.validate(tar)
    def test_normal_repository_links_and_hardlinks(self):
        names=self.validate([('node_modules/tool/bin.js','file',b'fixture'),('node_modules/.bin/tool','sym','../tool/bin.js'),('copy','hard','node_modules/tool/bin.js')])
        self.assertIn('node_modules/.bin/tool',names)
    def test_traversal_links_and_special_files_rejected(self):
        fixtures=[ [('a','sym','../outside')], [('a','sym','/etc/passwd')], [('a','sym','a')],
            [('a','sym','target'),('a/file','file',b'x')], [('x','special','')], [('a','hard','absent')],
            [('a/b','sym','..'),('a/c','sym','b/../../outside')], [('../outside','file',b'x')],
            [('same','file',b'x'),('same','sym','safe')] ]
        for entries in fixtures:
            with self.subTest(entries=entries),self.assertRaises(ValueError):self.validate(entries)
    def test_bad_bundle_cannot_change_environment_or_create_volumes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);env=root/'.env';env.write_text('PL_VOLUME=keep-me\n')
            backup=root/'bad.tgz';backup.write_bytes(archive([('../escape','file',b'x')]).getvalue())
            with patch.object(restore,'docker') as docker,self.assertRaises(ValueError):restore.restore(backup,env)
            docker.assert_not_called();self.assertEqual(env.read_text(),'PL_VOLUME=keep-me\n')
    def test_extraction_failure_keeps_previous_selection(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);env=root/'.env';env.write_text('PL_VOLUME=original\n')
            backup=root/'box.tgz';backup.write_bytes(archive([('box.json','file',b'{}'),('secrets.json','file',b'{}')]).getvalue())
            def fail(*args,**kw):
                if args[0]=='run':raise OSError('fixture failure')
                return 'new-volume'
            with patch.object(restore,'docker',side_effect=fail),patch('builtins.input',return_value='y'),patch.object(restore.subprocess,'run') as cleanup,self.assertRaises(OSError):restore.restore(backup,env)
            self.assertEqual(env.read_text(),'PL_VOLUME=original\n');self.assertEqual(cleanup.call_count,1)
            self.assertNotIn('original',str(cleanup.call_args))

if __name__=='__main__':unittest.main()
