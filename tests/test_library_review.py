"""旧因子复核不得靠反向、改家族规模或确认收益挑代表。"""

import copy
import unittest
from factor_miner.library_review import review_candidate, select_representatives

GATES = {"each_period_abs_rank_ic_min": .01, "each_period_bonferroni_p_max_exclusive": .05,
         "each_period_coverage_min": .8, "family_size": 30, "construct_status": "基础检验符合"}


def candidate(fid="huan001", t=4, hypothesis="H01"):
    """合成双区间候选。"""
    return {"factor_id": fid, "candidate_id": fid, "slot": int(fid[4:]), "hypothesis_id": hypothesis,
            "hypothesis_direction": "positive", "construct_validation": {"status": "基础检验符合"},
            **{name: {"rank_ic_mean": .02, "rank_ic_hac_t": t, "bonferroni_p_value": .01,
                      "median_coverage": .9} for name in ("discovery", "evaluation")}}


class LibraryReviewTest(unittest.TestCase):
    def test_opposite_sign_is_rejected_and_missing_is_not_zero(self):
        r = candidate()
        r["evaluation"]["rank_ic_mean"] = -.05
        self.assertEqual(review_candidate(r, GATES)["status"], "rejected")
        r["evaluation"]["rank_ic_mean"] = None
        with self.assertRaises(ValueError):
            review_candidate(r, GATES)

    def test_unverified_construct_is_watch_and_bad_returns_do_not_reject_feature(self):
        r = candidate()
        r["reference_metrics"] = {"annualized_return": -.5}
        self.assertEqual(review_candidate(r, GATES)["status"], "eligible")
        r["construct_validation"]["status"] = "证据不足"
        self.assertEqual(review_candidate(r, GATES)["status"], "watch")

    def test_representative_uses_discovery_and_keeps_distinct_factor(self):
        a, b, c = candidate(), candidate("huan002", 5, "H02"), candidate("huan003", 3, "H03")
        a["evaluation"]["rank_ic_hac_t"] = 100
        pairs = {tuple(sorted((x["factor_id"], y["factor_id"]))):
                 {"valid_dates": 700, "p95_abs_spearman": corr}
                 for x, y, corr in [(a, b, .9), (a, c, .2), (b, c, .3)]}
        reports = [a, b, c]
        before = copy.deepcopy(reports)
        reviews = {r["factor_id"]: review_candidate(r, GATES) for r in reports}
        self.assertEqual(select_representatives(reports, reviews, pairs, .75), ["huan002", "huan003"])
        self.assertEqual(reviews["huan001"]["representative"], "huan002")
        self.assertEqual(reports, before)
