import unittest

from eutonos import boards, goal_records as goals, tokenslang as ts


class GoalRecordTests(unittest.TestCase):
    def goal(self):
        draft = {'title': 'Useful outcome', 'summary': 'Bounded reason', 'extensions': {'goal': {
            'profile': goals.GOAL, 'outcome': 'The outcome is achieved', 'criteria': [
                {'key': 'result', 'statement': 'Evidence supports the result', 'evidence_refs': [],
                 'assessment': 'unassessed'}], 'scope': ['Current scope'], 'exclusions': [], 'work_links': []}}}
        return goals.form('goal', draft)['item']

    def test_frozen_ids_and_legacy_read_conversion(self):
        first, second = self.goal(), self.goal()
        self.assertNotEqual(first['id'], second['id'])
        self.assertEqual(boards._item(first), first)
        legacy = {'id': 'goal-existing', 'type': 'goal', 'title': 'Existing', 'summary': 'Old', 'status': 'completed'}
        self.assertEqual(boards._item(legacy), legacy)
        with self.assertRaises(ts.ReadError):
            boards._validate_written_work_item(legacy)

    def test_completion_is_explicit_supported_assessment(self):
        item = self.goal()
        item['status'] = 'completed'
        semantic = item['extensions']['goal']
        with self.assertRaises(ts.ReadError):
            goals.validate_goal(item, writing=True)
        semantic['outcome_assessment'] = {'assessment': 'supported', 'basis': 'Observed result'}
        with self.assertRaises(ts.ReadError):
            goals.validate_goal(item, writing=True)
        semantic['criteria'][0].update(assessment='supported', basis='Measured evidence')
        with self.assertRaises(ts.ReadError):
            goals.validate_goal(item, writing=True)
        semantic['criteria'][0]['evidence_refs'] = [{'path': 'evidence.md'}]
        goals.validate_goal(item, writing=True)
        semantic['criteria'][0]['assessment'] = 'contradicted'
        with self.assertRaises(ts.ReadError):
            goals.validate_goal(item, writing=True)
        item['status'] = 'active'
        goals.validate_goal(item, writing=True)

    def test_duplicate_criterion_and_parent_are_rejected(self):
        item = self.goal()
        semantic = item['extensions']['goal']
        semantic['criteria'].append(dict(semantic['criteria'][0]))
        with self.assertRaises(ts.ReadError):
            goals.validate_goal(item)
        semantic['criteria'].pop()
        item['parent_id'] = 'campaign-existing'
        with self.assertRaises(ts.ReadError):
            goals.validate_goal(item)

    def test_project_is_private_and_completion_requires_basis(self):
        item = goals.form('project', {'title': 'Kitchen', 'summary': 'Renovation'}, private=True)['item']
        item['extensions']['personal_project']['purpose'] = 'Make kitchen usable'
        goals.validate_project(item, writing=True, private=True)
        with self.assertRaises(ts.ReadError):
            boards._validate_written_work_item(item)
        item['status'] = 'completed'
        with self.assertRaises(ts.ReadError):
            goals.validate_project(item, writing=True, private=True)
        item['extensions']['personal_project']['completion_basis'] = 'Owner accepted result'
        goals.validate_project(item, writing=True, private=True)


if __name__ == '__main__':
    unittest.main()
