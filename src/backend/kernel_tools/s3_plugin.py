"""S3/CloudFront plugin - proxy to existing infrastructure."""
import logging
from typing import Annotated, Optional
from semantic_kernel.functions import kernel_function
from adapters.aws_adapter import AWSAdapter
from adapters.base_adapter import BaseAdapter


class S3Plugin:
    """S3/CloudFront plugin - calls existing infrastructure, not recreates it."""

    def __init__(self, project_id: str, session_id: Optional[str] = None, user_id: Optional[str] = None):
        self.project_id = project_id
        self.session_id = session_id
        self.user_id = user_id
        self._adapter = AWSAdapter(
            project_id=project_id,
            session_id=session_id,
            user_id=user_id
        )

    @kernel_function(
        name="get_video_signed_url",
        description="Get signed CloudFront URL for a video using S3 key"
    )
    async def get_signed_url(
        self,
        s3_key: Annotated[str, "S3 key (e.g., courses/xxx/lessons/yyy/video.mp4)"],
    ) -> str:
        try:
            result = await self._adapter.execute(
                tool_name="get_signed_url",
                params={"s3_key": s3_key},
                tool_id="get_video_signed_url"
            )
            
            if not result.success:
                if result.credentials_required:
                    return BaseAdapter.to_json(result)
                return f"Error: {result.error}"
            
            data = result.result
            return (
                f"##### Video Signed URL\n"
                f"**S3 Key:** {s3_key}\n"
                f"**URL:** {data.get('url')}\n"
                f"**Expires:** {data.get('expires_at', 'N/A')}\n"
                f"**Cache Status:** {data.get('cache_status', 'unknown')}\n"
                f"**Source:** {data.get('source', 'unknown')}\n"
                f"**Endpoint Used:** {data.get('endpoint_used', 'N/A')}\n"
                f"**Tool Call Status:** success\n"
            )
        except Exception as e:
            logging.error(f"S3 get_signed_url failed: {e}")
            return f"Error: {str(e)}"

    @kernel_function(
        name="s3_upload_object",
        description="Upload an object to S3 bucket. Required params: bucket_name, s3_key, content."
    )
    async def s3_upload_object(
        self,
        bucket_name: Annotated[str, "S3 bucket name"],
        s3_key: Annotated[str, "S3 key where the object will be stored"],
        content: Annotated[str, "Content to upload"],
    ) -> str:
        try:
            result = await self._adapter.execute(
                tool_name="s3_upload_object",
                params={"bucket_name": bucket_name, "s3_key": s3_key, "content": content},
                tool_id="s3_upload_object"
            )
            
            if not result.success:
                if result.credentials_required:
                    return BaseAdapter.to_json(result)
                return f"Error: {result.error}"
            
            data = result.result
            return f"Successfully uploaded object to {data.get('key')} in bucket {data.get('bucket')}"
        except Exception as e:
            logging.error(f"S3 upload_object failed: {e}")
            return f"Error: {str(e)}"

    @kernel_function(
        name="s3_delete_object",
        description="Delete an object from S3 bucket. Required params: bucket_name, s3_key."
    )
    async def s3_delete_object(
        self,
        bucket_name: Annotated[str, "S3 bucket name"],
        s3_key: Annotated[str, "S3 key of the object to delete"],
    ) -> str:
        try:
            result = await self._adapter.execute(
                tool_name="s3_delete_object",
                params={"bucket_name": bucket_name, "s3_key": s3_key},
                tool_id="s3_delete_object"
            )
            
            if not result.success:
                if result.credentials_required:
                    return BaseAdapter.to_json(result)
                return f"Error: {result.error}"
            
            data = result.result
            return f"Successfully deleted object {data.get('key')} from bucket {data.get('bucket')}"
        except Exception as e:
            logging.error(f"S3 delete_object failed: {e}")
            return f"Error: {str(e)}"

    @kernel_function(
        name="s3_list_objects",
        description="List objects in S3 bucket with optional prefix filter. Required param: bucket_name."
    )
    async def s3_list_objects(
        self,
        bucket_name: Annotated[str, "S3 bucket name"],
        prefix: Annotated[str, "Prefix to filter objects (optional)"] = "",
    ) -> str:
        try:
            result = await self._adapter.execute(
                tool_name="s3_list_objects",
                params={"bucket_name": bucket_name, "prefix": prefix},
                tool_id="s3_list_objects"
            )
            
            if not result.success:
                if result.credentials_required:
                    return BaseAdapter.to_json(result)
                return f"Error: {result.error}"
            
            data = result.result
            objects = data.get('objects', [])
            return f"Found {len(objects)} objects in bucket {data.get('bucket')} with prefix '{prefix}': {objects}"
        except Exception as e:
            logging.error(f"S3 list_objects failed: {e}")
            return f"Error: {str(e)}"
