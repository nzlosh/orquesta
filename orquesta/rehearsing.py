# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import os
import six
import unittest
import yaml

from six.moves import queue

from orquesta import conducting
from orquesta import constants
from orquesta import events
from orquesta import exceptions as exc
from orquesta import requests
from orquesta.specs import loader as spec_loader
from orquesta.specs.native.v1 import base as native_v1_specs
from orquesta.specs import types as spec_types
from orquesta import statuses
from orquesta.tests.fixtures import loader as fixture_loader


def load_test_spec(fixture):
    if not fixture:
        raise ValueError("Workflow test case is empty.")

    if isinstance(fixture, six.string_types):
        fixture = yaml.safe_load(fixture)

    if not isinstance(fixture, dict):
        raise ValueError("Unable to convert workflow test case into dict.")

    test_spec = (
        WorkflowRerunTestCase(fixture) if "workflow_state" in fixture else WorkflowTestCase(fixture)
    )

    test_spec.inspect(raise_exception=True)

    return WorkflowRehearsal(test_spec)


class MockInspectionError(native_v1_specs.Spec):
    _schema = {
        "type": "object",
        "properties": {
            "type": spec_types.NONEMPTY_STRING,
            "expression": spec_types.NONEMPTY_STRING,
            "message": spec_types.NONEMPTY_STRING,
            "schema_path": spec_types.NONEMPTY_STRING,
            "spec_path": spec_types.NONEMPTY_STRING,
        },
        "additionalProperties": False,
        "required": ["message"],
    }


class MockInspectionErrorSequenceSpec(native_v1_specs.SequenceSpec):
    _schema = {"type": "array", "items": MockInspectionError}


class MockInspectionErrors(native_v1_specs.Spec):
    _schema = {
        "type": "object",
        "properties": {
            "syntax": MockInspectionErrorSequenceSpec,
            "semantics": MockInspectionErrorSequenceSpec,
            "expressions": MockInspectionErrorSequenceSpec,
            "context": MockInspectionErrorSequenceSpec,
            "contents": MockInspectionErrorSequenceSpec,
        },
        "additionalProperties": False,
        "default": {},
    }


class MockWorkflowError(native_v1_specs.Spec):
    _schema = {
        "type": "object",
        "properties": {
            "type": spec_types.NONEMPTY_STRING,
            "message": spec_types.NONEMPTY_STRING,
            "task_id": spec_types.NONEMPTY_STRING,
            "route": {"type": "integer", "minimum": 0},
            "task_transition_id": spec_types.NONEMPTY_STRING,
            "result": spec_types.ANY,
            "data": {"type": "object"},
        },
        "additionalProperties": False,
        "required": ["type", "message"],
    }


class MockWorkflowErrorSequenceSpec(native_v1_specs.SequenceSpec):
    _schema = {"type": "array", "items": MockWorkflowError}


class MockActionExecution(native_v1_specs.Spec):
    _schema = {
        "type": "object",
        "properties": {
            "task_id": spec_types.NONEMPTY_STRING,
            "route": {"type": "integer", "minimum": 0, "default": 0},
            "seq_id": spec_types.POSITIVE_INTEGER,
            "item_id": spec_types.POSITIVE_INTEGER,
            "iter_id": {"type": "integer", "minimum": 0, "default": 0},
            "num_iter": {"type": "integer", "minimum": 0, "default": 1},
            "status": spec_types.WORKFLOW_STATUSES,
            "result": spec_types.ANY_NULLABLE,
            "result_path": spec_types.NONEMPTY_STRING,
        },
        "additionalProperties": False,
        "required": ["task_id"],
    }

    def __init__(self, *args, **kwargs):
        super(MockActionExecution, self).__init__(*args, **kwargs)

        if not getattr(self, "status", None):
            self.status = statuses.SUCCEEDED

        self.iter_pos = self.iter_id - 1


class MockActionExecutionSequenceSpec(native_v1_specs.SequenceSpec):
    _schema = {"type": "array", "items": MockActionExecution, "default": []}


class WorkflowTestCaseMixin(object):
    def get_mock_action_execution(self, task_id, item_id=None, seq_id=None):
        ac_exs = [
            x
            for x in self.mock_action_executions
            if x.task_id == task_id and x.iter_pos < x.iter_id + x.num_iter - 1
        ]

        if ac_exs and seq_id is not None:
            ac_exs = [x for x in ac_exs if x.seq_id == seq_id]

        if ac_exs and item_id is not None:
            ac_exs = [x for x in ac_exs if x.item_id == item_id]

        if len(ac_exs) > 0 and ac_exs[0].seq_id is not None and seq_id is None:
            return None

        return ac_exs[0] if len(ac_exs) > 0 else None


class WorkflowTestCase(native_v1_specs.Spec, WorkflowTestCaseMixin):
    _schema = {
        "type": "object",
        "properties": {
            "workflow": spec_types.NONEMPTY_STRING,
            "inputs": {"type": "object", "default": {}},
            "expected_inspection_errors": MockInspectionErrors,
            "expected_routes": {"type": "array", "items": {"type": "array"}, "default": [[]]},
            "expected_task_sequence": {"type": "array", "items": spec_types.NONEMPTY_STRING},
            "mock_action_executions": MockActionExecutionSequenceSpec,
            "expected_term_tasks": {"type": "array", "items": spec_types.NONEMPTY_STRING},
            "expected_workflow_status": spec_types.WORKFLOW_STATUSES,
            "expected_errors": MockWorkflowErrorSequenceSpec,
            "expected_output": {"type": "object"},
        },
        "required": ["workflow", "expected_task_sequence"],
        "additionalProperties": False,
    }

    def __init__(self, spec, name=None, member=False):
        if not spec:
            raise ValueError("The workflow test case cannot be empty.")

        super(WorkflowTestCase, self).__init__(spec, name=name, member=member)

        self.spec_module_name = "native"

        if not os.path.isfile(self.workflow):
            error_message = 'Unable to open worklfow definition at "%s"' % self.workflow
            raise exc.WorkflowRehearsalError(error_message)

        with open(self.workflow, "r") as f:
            self.wf_def = f.read()

        if not getattr(self, "expected_workflow_status", None):
            self.expected_workflow_status = statuses.SUCCEEDED


class WorkflowRerunTestCase(native_v1_specs.Spec, WorkflowTestCaseMixin):
    _schema = {
        "type": "object",
        "properties": {
            "workflow_state": {"type": "object"},
            "rerun_tasks": requests.TaskRerunRequestSequenceSpec,
            "expected_inspection_errors": MockInspectionErrors,
            "expected_routes": {"type": "array", "items": {"type": "array"}, "default": [[]]},
            "expected_task_sequence": {"type": "array", "items": spec_types.NONEMPTY_STRING},
            "mock_action_executions": MockActionExecutionSequenceSpec,
            "expected_term_tasks": {"type": "array", "items": spec_types.NONEMPTY_STRING},
            "expected_workflow_status": spec_types.WORKFLOW_STATUSES,
            "expected_errors": MockWorkflowErrorSequenceSpec,
            "expected_output": {"type": "object"},
        },
        "required": ["workflow_state", "expected_task_sequence"],
        "additionalProperties": False,
    }

    def __init__(self, spec, name=None, member=False):
        if not spec:
            raise ValueError("The workflow rerun test case cannot be empty.")

        super(WorkflowRerunTestCase, self).__init__(spec, name=name, member=member)

        self.conductor = conducting.WorkflowConductor.deserialize(self.workflow_state)

        if not getattr(self, "expected_workflow_status", None):
            self.expected_workflow_status = statuses.SUCCEEDED


class WorkflowRehearsal(unittest.TestCase):
    def __init__(self, session, *args, **kwargs):
        super(WorkflowRehearsal, self).__init__(*args, **kwargs)

        if not session:
            raise exc.WorkflowRehearsalError("The session object is not provided.")

        if not isinstance(session, WorkflowTestCase) and not isinstance(
            session, WorkflowRerunTestCase
        ):
            raise exc.WorkflowRehearsalError(
                "The session object is not type of WorkflowTestCase or WorkflowRerunTestCase."
            )

        self.session = session
        self.inspection_errors = {}
        self.rerun = False

        if isinstance(session, WorkflowTestCase):
            self.spec_module = spec_loader.get_spec_module(session.spec_module_name)
            self.wf_spec = self.spec_module.instantiate(self.session.wf_def)
            self.conductor = None
        elif isinstance(session, WorkflowRerunTestCase):
            self.conductor = self.session.conductor
            self.spec_module = self.conductor.spec_module
            self.wf_spec = self.conductor.spec
            self.rerun = True

        for mock_ac_ex in self.session.mock_action_executions:
            if not self.wf_spec.tasks.has_task(mock_ac_ex.task_id):
                raise exc.InvalidTask(mock_ac_ex.task_id)

            task_spec = self.wf_spec.tasks.get_task(mock_ac_ex.task_id)

            if task_spec.has_items() and mock_ac_ex.item_id is None:
                msg = 'Mock action execution for with items task "%s" is misssing "item_id".'
                raise exc.WorkflowRehearsalError(msg % mock_ac_ex.task_id)

            if not mock_ac_ex.result_path:
                continue

            if not os.path.isfile(mock_ac_ex.result_path):
                msg = 'The result path "%s" for the mock action execution does not exist.'
                raise exc.WorkflowRehearsalError(msg % mock_ac_ex.result_path)

            name, ext = os.path.splitext(mock_ac_ex.result_path)

            with open(mock_ac_ex.result_path) as f:
                mock_ac_ex.result = (
                    fixture_loader.FIXTURE_EXTS[ext](f)
                    if ext in fixture_loader.FIXTURE_EXTS
                    else f.read()
                )

    def runTest(self):
        """Override runTest

        The WorkflowRehearsal will be called directly in other unit tests. In python 2.7, doing
        this will lead to error "no such test method runTest". Since WorkflowRehearsal uses
        unittest.TestCase function and does not implement any unit tests, we can override and
        pass runTest here.
        """
        pass

    def assert_spec_inspection(self):
        self.inspection_errors = self.wf_spec.inspect()
        self.assertDictEqual(self.inspection_errors, self.session.expected_inspection_errors)

    def assert_conducting_sequence(self):
        run_q = queue.Queue()
        items_task_accum_result = {}

        # Inspect workflow spec and check for errors.
        self.assert_spec_inspection()

        # If there is expected inspection errors, then skip the check for conducting sequences.
        if self.session.expected_inspection_errors:
            return

        if not self.rerun:
            # Instantiate a workflow conductor to check conducting sequences.
            self.conductor = conducting.WorkflowConductor(self.wf_spec, inputs=self.session.inputs)
            self.conductor.request_workflow_status(statuses.RUNNING)
        else:
            # Request workflow rerun and assert workflow status is running.
            self.conductor.request_workflow_rerun(task_requests=self.session.rerun_tasks)
            self.assertEqual(self.conductor.get_workflow_status(), statuses.RESUMING)

        # Get start tasks and being conducting workflow.
        for task in self.conductor.get_next_tasks():
            run_q.put(task)

        # Serialize workflow conductor to mock async execution.
        wf_conducting_state = self.conductor.serialize()

        # Process until there are not more tasks in queue.
        while not run_q.empty():
            # Deserialize workflow conductor to mock async execution.
            self.conductor = conducting.WorkflowConductor.deserialize(wf_conducting_state)

            # Process all the tasks in the run queue.
            while not run_q.empty():
                current_task = run_q.get()
                current_task_id = current_task["id"]
                current_task_route = current_task["route"]
                current_task_spec = self.wf_spec.tasks.get_task(current_task_id)

                # Set task status to running.
                ac_ex_event = events.ActionExecutionEvent(statuses.RUNNING)
                self.conductor.update_task_state(current_task_id, current_task_route, ac_ex_event)

                # Mock completion of the task and apply mock action execution if given.
                current_seq_id = len(self.conductor.workflow_state.sequence) - 1

                ac_ex = self.session.get_mock_action_execution(
                    current_task_id, seq_id=current_seq_id
                )

                if not ac_ex:
                    ac_ex = self.session.get_mock_action_execution(current_task_id)

                if not ac_ex:
                    ac_ex = MockActionExecution({"task_id": current_task_id})
                else:
                    ac_ex.iter_pos += 1

                if not current_task_spec.has_items():
                    ac_ex_event = events.ActionExecutionEvent(ac_ex.status, result=ac_ex.result)
                else:
                    if current_task_id not in items_task_accum_result:
                        items_task_accum_result[current_task_id] = []

                    len_accum_result = len(items_task_accum_result[current_task_id])

                    if ac_ex.item_id is None:
                        ac_ex.item_id = len_accum_result

                    if ac_ex.item_id > len_accum_result - 1:
                        placeholders = [None] * (ac_ex.item_id - len_accum_result + 1)
                        items_task_accum_result[current_task_id].extend(placeholders)

                    items_task_accum_result[current_task_id][ac_ex.item_id] = ac_ex.result

                    ac_ex_event = events.TaskItemActionExecutionEvent(
                        ac_ex.item_id,
                        ac_ex.status,
                        result=ac_ex.result,
                        accumulated_result=items_task_accum_result[current_task_id],
                    )

                self.conductor.update_task_state(current_task_id, current_task_route, ac_ex_event)

            # Identify the next set of tasks.
            for next_task in self.conductor.get_next_tasks():
                run_q.put(next_task)

            # Serialize workflow execution graph to mock async execution.
            wf_conducting_state = self.conductor.serialize()

        actual_task_seq = [
            constants.TASK_STATE_ROUTE_FORMAT % (entry["id"], str(entry["route"]))
            for entry in self.conductor.workflow_state.sequence
        ]

        expected_task_seq = [
            task_id if "__r" in task_id else constants.TASK_STATE_ROUTE_FORMAT % (task_id, str(0))
            for task_id in self.session.expected_task_sequence
        ]

        self.assertListEqual(actual_task_seq, expected_task_seq)
        self.assertListEqual(self.conductor.workflow_state.routes, self.session.expected_routes)

        if self.conductor.get_workflow_status() in statuses.COMPLETED_STATUSES:
            self.conductor.render_workflow_output()

        self.assertEqual(
            self.conductor.get_workflow_status(), self.session.expected_workflow_status
        )

        if self.session.expected_errors is not None:
            self.assertListEqual(self.conductor.errors, self.session.expected_errors.spec)

        if self.session.expected_output is not None:
            self.assertDictEqual(self.conductor.get_workflow_output(), self.session.expected_output)

        if self.session.expected_term_tasks is not None:
            actual_term_tasks = [
                constants.TASK_STATE_ROUTE_FORMAT % (t["id"], str(t["route"]))
                for i, t in self.conductor.workflow_state.get_terminal_tasks()
            ]

            expected_term_tasks = [
                task_id
                if "__r" in task_id
                else constants.TASK_STATE_ROUTE_FORMAT % (task_id, str(0))
                for task_id in self.session.expected_term_tasks
            ]

            self.assertListEqual(sorted(actual_term_tasks), sorted(expected_term_tasks))
