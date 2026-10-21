import unittest
from fleet_core import transition, project, FleetError

class FleetCoreTests(unittest.TestCase):
    def setUp(self):
        self.draft={'id':'fixture','state':'DRAFT','photos':[]}
    def test_photo_duplicate_is_not_appended_and_input_is_unchanged(self):
        photo={'file_id':'synthetic','file_unique_id':'same'}
        one=transition(self.draft,'photo',photo,'2026-10-21')
        two=transition(one,'photo',photo,'2026-10-21')
        self.assertEqual(len(two['photos']),1)
        self.assertEqual(self.draft['photos'],[])
    def test_required_fields_and_future_date(self):
        for action,value in [('confirm',None),('date','2026-10-22'),('outlet','YOYI1'),('decision','VALID')]:
            with self.assertRaises(FleetError): transition(self.draft,action,value,'2026-10-21')
    def test_confirm_preserves_date_remark_and_media(self):
        r=self.draft
        for action,value in [('date','2026-10-19'),('outlet','SBH307'),('decision','OFFLOAD'),('remark','Existing issue'),('photo',{'file_id':'synthetic','file_unique_id':'one'})]:
            r=transition(r,action,value,'2026-10-21')
        claimed=transition(r,'confirm',None,'2026-10-21')
        self.assertEqual(claimed['state'],'SENDING')
        self.assertEqual(claimed['reportDate'],'2026-10-19')
        self.assertEqual(r['state'],'DRAFT')
        with self.assertRaises(FleetError): transition(claimed,'confirm',None,'2026-10-21')
    def test_only_sent_projects_link_no_secrets(self):
        self.assertIsNone(project(self.draft,'-1001234567890'))
        r={**self.draft,'state':'SENT','archiveMessageIds':[42],'photos':[{'file_id':'private'}]}
        result=project(r,'-1001234567890')
        self.assertEqual(result['telegramUrl'],'https://t.me/c/1234567890/42')
        self.assertNotIn('private',str(result))
        with self.assertRaises(FleetError): project(r,'-123')
    def test_max_photos_does_not_remove_existing(self):
        r=self.draft
        for i in range(5): r=transition(r,'photo',{'file_id':str(i),'file_unique_id':str(i)},'2026-10-21')
        with self.assertRaises(FleetError): transition(r,'photo',{'file_id':'6','file_unique_id':'6'},'2026-10-21')
        self.assertEqual(len(r['photos']),5)

if __name__=='__main__': unittest.main()
