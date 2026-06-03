# Copyright Amazon.com Inc. or its affiliates. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License"). You may
# not use this file except in compliance with the License. A copy of the
# License is located at
#
#	 http://aws.amazon.com/apache2.0/
#
# or in the "license" file accompanying this file. This file is distributed
# on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either
# express or implied. See the License for the specific language governing
# permissions and limitations under the License.
"""Integration tests for the S3 Tables TableBucket resource."""

import time

import pytest

from acktest.k8s import condition
from acktest.k8s import resource as k8s
from acktest.resources import random_suffix_name

from e2e import (
    CRD_GROUP,
    CRD_VERSION,
    load_s3tables_resource,
    service_marker,
)
from e2e.replacement_values import REPLACEMENT_VALUES

RESOURCE_PLURAL = "tablebuckets"

CREATE_WAIT_AFTER_SECONDS = 20
MODIFY_WAIT_AFTER_SECONDS = 20
DELETE_WAIT_AFTER_SECONDS = 20


def get_table_bucket(s3tables_client, arn: str):
    """Returns the table bucket from AWS, or None if it does not exist."""
    try:
        return s3tables_client.get_table_bucket(tableBucketARN=arn)
    except s3tables_client.exceptions.NotFoundException:
        return None


@service_marker
@pytest.mark.canary
class TestTableBucket:
    def test_create_update_delete(self, s3tables_client):
        table_bucket_name = random_suffix_name("ack-test-bucket", 32)

        replacements = REPLACEMENT_VALUES.copy()
        replacements["TABLE_BUCKET_NAME"] = table_bucket_name

        resource_data = load_s3tables_resource(
            "table_bucket",
            additional_replacements=replacements,
        )

        ref = k8s.CustomResourceReference(
            CRD_GROUP,
            CRD_VERSION,
            RESOURCE_PLURAL,
            table_bucket_name,
            namespace="default",
        )
        k8s.create_custom_resource(ref, resource_data)
        cr = k8s.wait_resource_consumed_by_controller(ref)

        assert cr is not None
        assert k8s.get_resource_exists(ref)

        time.sleep(CREATE_WAIT_AFTER_SECONDS)

        # The resource should reach a Synced=True condition.
        assert k8s.wait_on_condition(
            ref,
            condition.CONDITION_TYPE_RESOURCE_SYNCED,
            "True",
            wait_periods=10,
        )

        cr = k8s.get_resource(ref)
        assert "status" in cr
        assert "ackResourceMetadata" in cr["status"]
        arn = cr["status"]["ackResourceMetadata"]["arn"]
        assert arn is not None

        # AWS-assigned status fields populated from GetTableBucket and the
        # standard ACKResourceMetadata. The ownerAccountID of an ACK-managed
        # resource lives under the common ackResourceMetadata block.
        assert cr["status"]["ackResourceMetadata"]["ownerAccountID"] is not None
        assert cr["status"].get("createdAt") is not None

        # Verify the table bucket exists in AWS.
        aws_bucket = get_table_bucket(s3tables_client, arn)
        assert aws_bucket is not None
        assert aws_bucket["name"] == table_bucket_name

        # Tags supplied at creation should be applied in AWS.
        tags = s3tables_client.list_tags_for_resource(resourceArn=arn)["tags"]
        assert tags.get("environment") == "test"
        assert tags.get("team") == "ack"

        # The bucket should start with the default STANDARD storage class.
        sc = s3tables_client.get_table_bucket_storage_class(tableBucketARN=arn)
        assert sc["storageClassConfiguration"]["storageClass"] == "STANDARD"

        # Update 1: set a non-default storage class via the dedicated Put API
        # (exercises customUpdateTableBucket / syncStorageClass).
        updates = {
            "spec": {
                "storageClassConfiguration": {
                    "storageClass": "INTELLIGENT_TIERING",
                },
            },
        }
        k8s.patch_custom_resource(ref, updates)
        time.sleep(MODIFY_WAIT_AFTER_SECONDS)

        assert k8s.wait_on_condition(
            ref,
            condition.CONDITION_TYPE_RESOURCE_SYNCED,
            "True",
            wait_periods=10,
        )

        # Verify the storage class change is reflected in AWS.
        sc = s3tables_client.get_table_bucket_storage_class(tableBucketARN=arn)
        assert sc["storageClassConfiguration"]["storageClass"] == "INTELLIGENT_TIERING"

        # Update 2: change tags. The CR is patched with a JSON merge patch, so a
        # tag is only removed when its key is explicitly set to null. We change
        # an existing tag's value (environment), add a new tag (owner), and
        # remove a tag (team -> null). This exercises both TagResource and
        # UntagResource without looping.
        tag_updates = {
            "spec": {
                "tags": {
                    "environment": "prod",
                    "owner": "platform",
                    "team": None,
                },
            },
        }
        k8s.patch_custom_resource(ref, tag_updates)
        time.sleep(MODIFY_WAIT_AFTER_SECONDS)

        assert k8s.wait_on_condition(
            ref,
            condition.CONDITION_TYPE_RESOURCE_SYNCED,
            "True",
            wait_periods=10,
        )

        # The controller also manages its own `services.k8s.aws/*` tags, so we
        # assert only on the user-defined tags rather than exact-matching.
        tags = s3tables_client.list_tags_for_resource(resourceArn=arn)["tags"]
        assert tags.get("environment") == "prod"
        assert tags.get("owner") == "platform"
        assert "team" not in tags

        # Delete the resource and confirm it is removed from AWS.
        _, deleted = k8s.delete_custom_resource(ref)
        assert deleted
        time.sleep(DELETE_WAIT_AFTER_SECONDS)

        assert get_table_bucket(s3tables_client, arn) is None
