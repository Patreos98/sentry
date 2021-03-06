import responses
from urllib.parse import parse_qsl
from sentry.utils.compat.mock import patch

from sentry.utils import json
from sentry.incidents.logic import CRITICAL_TRIGGER_LABEL
from sentry.integrations.slack.utils import build_group_attachment, build_incident_attachment
from sentry.models import Integration, OrganizationIntegration
from sentry.testutils import APITestCase
from sentry.testutils.helpers.datetime import iso_format, before_now
from sentry.utils.compat import filter

UNSET = object()

LINK_SHARED_EVENT = """{
    "type": "link_shared",
    "channel": "Cxxxxxx",
    "user": "Uxxxxxxx",
    "message_ts": "123456789.9875",
    "team_id": "TXXXXXXX1",
    "links": [
        {
            "domain": "example.com",
            "url": "http://testserver/fizz/buzz"
        },
        {
            "domain": "example.com",
            "url": "http://testserver/organizations/%(org1)s/issues/%(group1)s/"
        },
        {
            "domain": "example.com",
            "url": "http://testserver/organizations/%(org2)s/issues/%(group2)s/bar/"
        },
        {
            "domain": "example.com",
            "url": "http://testserver/organizations/%(org1)s/issues/%(group1)s/bar/"
        },
        {
            "domain": "example.com",
            "url": "http://testserver/organizations/%(org1)s/issues/%(group3)s/events/%(event)s/"
        },
        {
            "domain": "example.com",
            "url": "http://testserver/organizations/%(org1)s/incidents/%(incident)s/"
        },
        {
            "domain": "another-example.com",
            "url": "https://yet.another-example.com/v/abcde"
        }
    ]
}"""

MESSAGE_IM_EVENT = """{
    "type": "message",
    "channel": "DOxxxxxx",
    "user": "Uxxxxxxx",
    "text": "helloo",
    "message_ts": "123456789.9875"
}"""

MESSAGE_IM_BOT_EVENT = """{
    "type": "message",
    "channel": "DOxxxxxx",
    "user": "Uxxxxxxx",
    "text": "helloo",
    "bot_id": "bot_id",
    "message_ts": "123456789.9875"
}"""


class BaseEventTest(APITestCase):
    def setUp(self):
        super().setUp()
        self.user = self.create_user(is_superuser=False)
        self.org = self.create_organization(owner=None)
        self.integration = Integration.objects.create(
            provider="slack",
            external_id="TXXXXXXX1",
            metadata={"access_token": "xoxp-xxxxxxxxx-xxxxxxxxxx-xxxxxxxxxxxx"},
        )
        OrganizationIntegration.objects.create(organization=self.org, integration=self.integration)

    @patch(
        "sentry.integrations.slack.requests.SlackRequest._check_signing_secret", return_value=True
    )
    def post_webhook(
        self,
        check_signing_secret_mock,
        event_data=None,
        type="event_callback",
        data=None,
        token=UNSET,
        team_id="TXXXXXXX1",
    ):
        payload = {
            "team_id": team_id,
            "api_app_id": "AXXXXXXXX1",
            "type": type,
            "authed_users": [],
            "event_id": "Ev08MFMKH6",
            "event_time": 123456789,
        }
        if data:
            payload.update(data)
        if event_data:
            payload.setdefault("event", {}).update(event_data)

        return self.client.post("/extensions/slack/event/", payload)


class UrlVerificationEventTest(BaseEventTest):
    challenge = "3eZbrw1aBm2rZgRNFdxV2595E9CY3gmdALWMmHkvFXO7tYXAYM8P"

    @patch(
        "sentry.integrations.slack.requests.SlackRequest._check_signing_secret", return_value=True
    )
    def test_valid_event(self, check_signing_secret_mock):
        resp = self.client.post(
            "/extensions/slack/event/",
            {
                "type": "url_verification",
                "challenge": self.challenge,
            },
        )
        assert resp.status_code == 200, resp.content
        assert resp.data["challenge"] == self.challenge


class LinkSharedEventTest(BaseEventTest):
    @responses.activate
    def test_valid_token(self):
        responses.add(responses.POST, "https://slack.com/api/chat.unfurl", json={"ok": True})
        org2 = self.create_organization(name="biz")
        project1 = self.create_project(organization=self.org)
        project2 = self.create_project(organization=org2)
        min_ago = iso_format(before_now(minutes=1))
        group1 = self.create_group(project=project1)
        group2 = self.create_group(project=project2)
        event = self.store_event(
            data={"fingerprint": ["group3"], "timestamp": min_ago}, project_id=project1.id
        )
        group3 = event.group
        alert_rule = self.create_alert_rule()

        incident = self.create_incident(
            status=2, organization=self.org, projects=[project1], alert_rule=alert_rule
        )
        incident.update(identifier=123)
        trigger = self.create_alert_rule_trigger(alert_rule, CRITICAL_TRIGGER_LABEL, 100)
        action = self.create_alert_rule_trigger_action(
            alert_rule_trigger=trigger, triggered_for_incident=incident
        )

        resp = self.post_webhook(
            event_data=json.loads(
                LINK_SHARED_EVENT
                % {
                    "group1": group1.id,
                    "group2": group2.id,
                    "group3": group3.id,
                    "incident": incident.identifier,
                    "org1": self.org.slug,
                    "org2": org2.slug,
                    "event": event.event_id,
                }
            )
        )
        assert resp.status_code == 200, resp.content
        data = dict(parse_qsl(responses.calls[0].request.body))
        unfurls = json.loads(data["unfurls"])
        issue_url = f"http://testserver/organizations/{self.org.slug}/issues/{group1.id}/bar/"
        incident_url = (
            f"http://testserver/organizations/{self.org.slug}/incidents/{incident.identifier}/"
        )
        event_url = f"http://testserver/organizations/{self.org.slug}/issues/{group3.id}/events/{event.event_id}/"

        assert unfurls == {
            issue_url: build_group_attachment(group1),
            incident_url: build_incident_attachment(action, incident),
            event_url: build_group_attachment(group3, event=event, link_to_event=True),
        }
        assert data["token"] == "xoxp-xxxxxxxxx-xxxxxxxxxx-xxxxxxxxxxxx"

    @responses.activate
    def test_user_access_token(self):
        # this test is needed to make sure that classic bots installed by on-prem users
        # still work since they needed to use a user_access_token for unfurl
        self.integration.metadata.update(
            {
                "user_access_token": "xoxt-xxxxxxxxx-xxxxxxxxxx-xxxxxxxxxxxx",
                "access_token": "xoxm-xxxxxxxxx-xxxxxxxxxx-xxxxxxxxxxxx",
            }
        )
        self.integration.save()
        responses.add(responses.POST, "https://slack.com/api/chat.unfurl", json={"ok": True})
        org2 = self.create_organization(name="biz")
        project1 = self.create_project(organization=self.org)
        project2 = self.create_project(organization=org2)
        min_ago = iso_format(before_now(minutes=1))
        group1 = self.create_group(project=project1)
        group2 = self.create_group(project=project2)
        event = self.store_event(
            data={"fingerprint": ["group3"], "timestamp": min_ago}, project_id=project1.id
        )
        group3 = event.group
        alert_rule = self.create_alert_rule()
        incident = self.create_incident(
            status=2, organization=self.org, projects=[project1], alert_rule=alert_rule
        )
        incident.update(identifier=123)
        resp = self.post_webhook(
            event_data=json.loads(
                LINK_SHARED_EVENT
                % {
                    "group1": group1.id,
                    "group2": group2.id,
                    "group3": group3.id,
                    "incident": incident.identifier,
                    "org1": self.org.slug,
                    "org2": org2.slug,
                    "event": event.event_id,
                }
            )
        )
        assert resp.status_code == 200, resp.content
        data = dict(parse_qsl(responses.calls[0].request.body))
        assert data["token"] == "xoxt-xxxxxxxxx-xxxxxxxxxx-xxxxxxxxxxxx"


def get_block_type_text(block_type, data):
    block = filter(lambda x: x["type"] == block_type, data["blocks"])[0]
    if block_type == "section":
        return block["text"]["text"]

    return block["elements"][0]["text"]["text"]


class MessageIMEventTest(BaseEventTest):
    @responses.activate
    def test_user_message_im(self):
        responses.add(responses.POST, "https://slack.com/api/chat.postMessage", json={"ok": True})
        resp = self.post_webhook(event_data=json.loads(MESSAGE_IM_EVENT))
        assert resp.status_code == 200, resp.content
        request = responses.calls[0].request
        assert request.headers["Authorization"] == "Bearer xoxp-xxxxxxxxx-xxxxxxxxxx-xxxxxxxxxxxx"
        data = json.loads(request.body)
        assert (
            get_block_type_text("section", data)
            == "Want to learn more about configuring alerts in Sentry? Check out our documentation."
        )
        assert get_block_type_text("actions", data) == "Sentry Docs"

    def test_bot_message_im(self):
        resp = self.post_webhook(event_data=json.loads(MESSAGE_IM_BOT_EVENT))
        assert resp.status_code == 200, resp.content
