import unittest

import app

app.INTERVAL = 60


def board(atg_c, ice_status):
    return f"""<table>
<tr><th>#</th><th>TEAM</th><th>POINTS</th><th>UPTIME</th><th>ATG</th><th>ICEBANK</th><th>LASER</th></tr>
<tr><td>1</td><td>krongdi<br><small>KPN</small></td><td>4276</td><td>97.81%</td>
  <td><span>&#9745;</span> OK<br>C {atg_c}<br>D 47<br>L 6</td>
  <td>{ice_status}<br>C 58<br>D 1<br>L 29</td>
  <td></td></tr>
</table>"""


class Test(unittest.TestCase):
    def test_parse(self):
        teams, svcs = app.parse(board(191, "FF"))
        self.assertEqual(teams, [("krongdi", 4276, 97.81)])
        self.assertEqual(svcs, [("krongdi", "ATG", "OK", 191, 47, 6), ("krongdi", "ICEBANK", "FF", 58, 1, 29)])

    def test_window_gains_and_sla(self):
        db = app.connect(":memory:")
        app.store(db, 0, board(100, "FF"))
        app.store(db, 600, board(150, "OK"))
        app.store(db, 1200, board(190, "OK"))
        last, teams, svcs = app.window_data(db, 10)
        self.assertEqual(last, 1200)
        atg, ice = sorted(svcs, key=lambda r: r[1])
        self.assertEqual(atg[6], 40)
        self.assertEqual(teams[0][4], 97.81)
        page = app.index(db, 60)
        self.assertIn("<span>+90</span>", page)
        self.assertIn("<span>97.81%</span>", page)
        history = app.graph(db, {"team": "krongdi", "service": "ICEBANK", "metric": "status"}, 10)
        self.assertIn("1 downtime periods", history)
        self.assertIn(">FF</td><td>1 min</td>", history)

    def test_spike(self):
        db = app.connect(":memory:")
        for k in range(7):
            app.store(db, k * 600, board(10 * k, "OK"))
        app.store(db, 4200, board(110, "OK"))
        _, _, svcs = app.window_data(db, 10)
        atg = [r for r in svcs if r[1] == "ATG"][0]
        self.assertEqual((atg[6], atg[9]), (50, 10))
        self.assertEqual(app.spike(50, 10), 5)
        self.assertIsNone(app.spike(14, 10))
        self.assertIsNone(app.spike(6, 2))
        self.assertIn("krongdi ATG C +50 (x5)", app.index(db, 10))

    def test_rate(self):
        self.assertEqual(app.rate([(0, 0), (60, 10), (120, 20)]), [(60, 10.0), (120, 10.0)])


if __name__ == "__main__":
    unittest.main()
