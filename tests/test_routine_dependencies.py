from contextlib import closing

import boto3
from boto3.s3.transfer import TransferConfig
from botocore.stub import Stubber


def test_updated_aws_stack_paginates_without_network_or_real_credentials():
    client = boto3.client(
        "s3", region_name="us-east-1",
        aws_access_key_id="example-access-key",
        aws_secret_access_key="example-secret-key",
    )
    with closing(client), Stubber(client) as stubber:
        stubber.add_response("list_objects_v2", {
            "IsTruncated": True,
            "NextContinuationToken": "next-page",
            "Contents": [{"Key": "crawl/first.arc.gz"}],
        }, {"Bucket": "commoncrawl", "Prefix": "crawl/"})
        stubber.add_response("list_objects_v2", {
            "IsTruncated": False,
            "Contents": [{"Key": "crawl/second.arc.gz"}],
        }, {"Bucket": "commoncrawl", "Prefix": "crawl/", "ContinuationToken": "next-page"})

        pages = client.get_paginator("list_objects_v2").paginate(
            Bucket="commoncrawl", Prefix="crawl/",
        )
        assert [row["Key"] for page in pages for row in page["Contents"]] == [
            "crawl/first.arc.gz", "crawl/second.arc.gz",
        ]
        stubber.assert_no_pending_responses()
    assert not TransferConfig(use_threads=False).use_threads
