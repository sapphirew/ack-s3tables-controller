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
"""Integration tests for the S3 Tables Table resource."""

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

TABLE_BUCKET_PLURAL = "tablebuckets"
NAMESPACE_PLURAL = "namespaces"
TABLE_PLURAL = "tables"

CREATE_WAIT_AFTER_SECONDS = 20
MODIFY_WAIT_AFTER_SECONDS = 20
DELETE_WAIT_AFTER_SECONDS = 20


def get_table(s3tables_client, table_bucket_arn: str, namespace: str, name: str):
    """Returns the table from AWS, or None if it does not exist."""
    try:
        return s3tables_client.get_table(
            tableBucketARN=table_bucket_arn, namespace=namespace, name=name
        )
    except s3tables_client.exceptions.NotFoundException:
        return None


@service_marker
@pytest.mark.canary
class TestTable:
    def test_create_update_delete(self, s3tables_client):
        # --- Setup: create TableBucket + Namespace prerequisites ---
        table_bucket_name = random_suffix_name("ack-test-bucket", 32)
        tb_replacements = REPLACEMENT_VALUES.copy()
        tb_replacements["TABLE_BUCKET_NAME"] = table_bucket_name
        tb_data = load_s3tables_resource(
            "table_bucket", additional_replacements=tb_replacements
        )
        tb_ref = k8s.CustomResourceReference(
            CRD_GROUP, CRD_VERSION, TABLE_BUCKET_PLURAL,
            table_bucket_name, namespace="default",
        )
        k8s.create_custom_resource(tb_ref, tb_data)
        k8s.wait_resource_consumed_by_controller(tb_ref)
        time.sleep(CREATE_WAIT_AFTER_SECONDS)
        assert k8s.wait_on_condition(
            tb_ref, condition.CONDITION_TYPE_RESOURCE_SYNCED, "True", wait_periods=20,
        )
        tb_cr = k8s.get_resource(tb_ref)
        table_bucket_arn = tb_cr["status"]["ackResourceMetadata"]["arn"]

        # Namespace names: ^[0-9a-z_]*$
        namespace_name = random_suffix_name("ack_test_ns", 24).replace("-", "_")
        ns_cr_name = namespace_name.replace("_", "-")
        ns_replacements = REPLACEMENT_VALUES.copy()
        ns_replacements["NAMESPACE_NAME"] = namespace_name
        ns_replacements["NAMESPACE_CR_NAME"] = ns_cr_name
        ns_replacements["TABLE_BUCKET_CR_NAME"] = table_bucket_name
        ns_data = load_s3tables_resource(
            "namespace", additional_replacements=ns_replacements
        )
        ns_ref = k8s.CustomResourceReference(
            CRD_GROUP, CRD_VERSION, NAMESPACE_PLURAL,
            ns_cr_name, namespace="default",
        )
        k8s.create_custom_resource(ns_ref, ns_data)
        k8s.wait_resource_consumed_by_controller(ns_ref)
        time.sleep(CREATE_WAIT_AFTER_SECONDS)
        assert k8s.wait_on_condition(
            ns_ref, condition.CONDITION_TYPE_RESOURCE_SYNCED, "True", wait_periods=20,
        )

        try:
            # --- Create the Table ---
            table_name = random_suffix_name("ack_test_tbl", 24).replace("-", "_")
            table_cr_name = table_name.replace("_", "-")
            replacements = REPLACEMENT_VALUES.copy()
            replacements["TABLE_NAME"] = table_name
            replacements["TABLE_CR_NAME"] = table_cr_name
            replacements["TABLE_BUCKET_CR_NAME"] = table_bucket_name
            replacements["NAMESPACE_CR_NAME"] = ns_cr_name

            table_data = load_s3tables_resource(
                "table", additional_replacements=replacements
            )
            table_ref = k8s.CustomResourceReference(
                CRD_GROUP, CRD_VERSION, TABLE_PLURAL,
                table_cr_name, namespace="default",
            )
            k8s.create_custom_resource(table_ref, table_data)
            cr = k8s.wait_resource_consumed_by_controller(table_ref)

            assert cr is not None
            assert k8s.get_resource_exists(table_ref)

            time.sleep(CREATE_WAIT_AFTER_SECONDS)

            assert k8s.wait_on_condition(
                table_ref, condition.CONDITION_TYPE_RESOURCE_SYNCED, "True", wait_periods=20,
            )

            cr = k8s.get_resource(table_ref)
            assert "status" in cr
            assert cr["status"]["ackResourceMetadata"]["ownerAccountID"] is not None
            assert cr["status"].get("createdAt") is not None
            assert cr["status"].get("versionToken") is not None

            # Verify in AWS.
            aws_table = get_table(
                s3tables_client, table_bucket_arn, namespace_name, table_name
            )
            assert aws_table is not None
            assert aws_table["name"] == table_name
            assert aws_table["format"] == "ICEBERG"

            table_arn = cr["status"].get("tableARN") or cr["status"]["ackResourceMetadata"]["arn"]

            # Tags applied at creation.
            tags = s3tables_client.list_tags_for_resource(resourceArn=table_arn)["tags"]
            assert tags.get("environment") == "test"
            assert tags.get("team") == "ack"

            # --- Update: tags ---
            tag_updates = {
                "spec": {
                    "tags": {
                        "environment": "prod",
                        "owner": "platform",
                        "team": None,
                    },
                },
            }
            k8s.patch_custom_resource(table_ref, tag_updates)
            time.sleep(MODIFY_WAIT_AFTER_SECONDS)

            assert k8s.wait_on_condition(
                table_ref, condition.CONDITION_TYPE_RESOURCE_SYNCED, "True", wait_periods=20,
            )

            tags = s3tables_client.list_tags_for_resource(resourceArn=table_arn)["tags"]
            assert tags.get("environment") == "prod"
            assert tags.get("owner") == "platform"
            assert "team" not in tags

            # --- Update: rename (exercises RenameTable + versionToken guard) ---
            new_table_name = random_suffix_name("ack_test_tbl", 24).replace("-", "_")
            rename_updates = {"spec": {"name": new_table_name}}
            k8s.patch_custom_resource(table_ref, rename_updates)
            time.sleep(MODIFY_WAIT_AFTER_SECONDS)

            assert k8s.wait_on_condition(
                table_ref, condition.CONDITION_TYPE_RESOURCE_SYNCED, "True", wait_periods=20,
            )

            # The new name resolves in AWS; the old name no longer exists.
            assert get_table(
                s3tables_client, table_bucket_arn, namespace_name, new_table_name
            ) is not None
            assert get_table(
                s3tables_client, table_bucket_arn, namespace_name, table_name
            ) is None
            table_name = new_table_name

            # --- Delete ---
            _, deleted = k8s.delete_custom_resource(table_ref)
            assert deleted
            time.sleep(DELETE_WAIT_AFTER_SECONDS)
            assert get_table(
                s3tables_client, table_bucket_arn, namespace_name, table_name
            ) is None
        finally:
            k8s.delete_custom_resource(ns_ref)
            time.sleep(DELETE_WAIT_AFTER_SECONDS)
            k8s.delete_custom_resource(tb_ref)
            time.sleep(DELETE_WAIT_AFTER_SECONDS)
