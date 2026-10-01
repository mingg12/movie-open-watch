import copy
import unittest
from watch import parse_cgv, parse_mega, parse_lotte, parse_cineq, unsent_sessions, mark_sent

CONFIG = {'date':'20261010', 'movie_keyword':'치이카와', 'cgv_movie_no':'30001367'}

class WatchTests(unittest.TestCase):
    def test_cgv_other_movie_and_sold_out_excluded(self):
        row = dict(siteNo='0046', scnYmd='20261010', movNo='30001367', scnsNo='01', scnsrtTm='0930', scnsNm='1관', frSeatCnt=12)
        body = {'statusCode':0, 'data':[row, dict(row, movNo='different'), dict(row, frSeatCnt=0)]}
        result = parse_cgv(body, {'code':'0046'}, CONFIG)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]['time'], '09:30')

    def test_failure_never_means_empty(self):
        for fn, body in [(parse_cgv, {'statusCode':500,'data':[]}), (parse_mega, {'statCd':0}), (parse_lotte, {'IsOK':'false'})]:
            with self.assertRaises(ValueError): fn(body, {'code':'0046'}, CONFIG)
        with self.assertRaises(ValueError): parse_cineq('<html>Access denied</html>', {'code':'2002'}, CONFIG)

    def test_wrong_date_branch_rejected(self):
        row = {'brchNo':'1311','playDe':'20261009','movieNm':'치이카와','restSeatCnt':10}
        with self.assertRaises(ValueError): parse_mega({'statCd':0,'movieFormList':[row]}, {'code':'1311'}, CONFIG)

    def test_lotte_remaining_seats_and_booking_flag(self):
        row = dict(CinemaID=1014, PlayDt='2026-10-10', MovieNameKR='극장판 치이카와', BookingSeatCount=75, IsBookingYN='Y', ScreenID=101404, PlaySequence=6, StartTime='09:30', ScreenNameKR='4관')
        result = parse_lotte({'IsOK':'true','PlaySeqs':{'Items':[row, dict(row,IsBookingYN='N'),dict(row,BookingSeatCount=0)]}}, {'code':'1014'},CONFIG)
        self.assertEqual(len(result),1)
        self.assertEqual(result[0]['seats'],75)

    def test_cineq_target_movie_only(self):
        text = '''<div class="priceclick"></div><div class="each-movie-time"><div class="title">극장판 치이카와</div><div class="screen"><div class="screen-name">1관</div><div class="time" data-theatercode="2002" data-playdate="20261010" data-screenplanid="123"><a>09:00<span class="to">~10:49</span><span class="seats-status">20 / 30</span></a></div></div></div>'''
        result = parse_cineq(text, {'code':'2002'}, CONFIG)
        self.assertEqual(result[0]['time'],'09:00')
        self.assertEqual(result[0]['seats'],20)
        self.assertEqual(parse_cineq(text.replace('치이카와','다른 영화'), {'code':'2002'}, CONFIG),[])

    def test_new_recipient_gets_existing_open_sessions(self):
        state = {}; sessions = [{'id':'a'}, {'id':'b'}]
        mark_sent(state,'first@example.com','t',sessions)
        self.assertEqual(unsent_sessions(state,'first@example.com','t',sessions),[])
        self.assertEqual(unsent_sessions(state,'second@example.com','t',sessions),sessions)
        self.assertEqual(unsent_sessions(state,'first@example.com','t',sessions+[{'id':'c'}]),[{'id':'c'}])
        self.assertNotIn('first@example.com',str(state))

    def test_delivery_state_only_changes_when_marked(self):
        state = {}; before = copy.deepcopy(state)
        unsent_sessions(state,'first@example.com','t',[{'id':'a'}])
        self.assertEqual(state,before)

if __name__ == '__main__': unittest.main()
