import importlib
import warnings
from typing import List, Optional

from letta.constants import BASE_MEMORY_TOOLS, BASE_TOOLS
from letta.functions.functions import derive_openai_json_schema, load_function_set
from letta.orm.enums import ToolType

# TODO: Remove this once we translate all of these to the ORM
from letta.orm.errors import NoResultFound
from letta.orm.tool import Tool as ToolModel
from letta.schemas.tool import Tool as PydanticTool
from letta.schemas.tool import ToolUpdate
from letta.schemas.user import User as PydanticUser
from letta.utils import enforce_types, printd


class ToolManager:
    """Manager class to handle business logic related to Tools."""

    BASE_TOOL_NAMES = [
        "send_message",
        "conversation_search",
        "archival_memory_insert",
        "archival_memory_search",
    ]
    BASE_MEMORY_TOOL_NAMES = ["core_memory_append", "core_memory_replace"]

    def __init__(self):
        # Fetching the db_context similarly as in OrganizationManager
        from letta.server.server import db_context

        self.session_maker = db_context

    # TODO: Refactor this across the codebase to use CreateTool instead of passing in a Tool object
    @enforce_types
    def create_or_update_tool(self, pydantic_tool: PydanticTool, actor: PydanticUser) -> PydanticTool:
        """Create a new tool based on the ToolCreate schema."""
        tool = self.get_tool_by_name(tool_name=pydantic_tool.name, actor=actor)
        if tool:
            # Put to dict and remove fields that should not be reset
            update_data = pydantic_tool.model_dump(exclude={"module"}, exclude_unset=True, exclude_none=True)

            # If there's anything to update
            if update_data:
                self.update_tool_by_id(tool.id, ToolUpdate(**update_data), actor)
            else:
                printd(
                    f"`create_or_update_tool` was called with user_id={actor.id}, organization_id={actor.organization_id}, name={pydantic_tool.name}, but found existing tool with nothing to update."
                )
        else:
            tool = self.create_tool(pydantic_tool, actor=actor)

        return tool

    @enforce_types
    def create_tool(self, pydantic_tool: PydanticTool, actor: PydanticUser) -> PydanticTool:
        """Create a new tool based on the ToolCreate schema."""
        with self.session_maker() as session:
            # Set the organization id at the ORM layer
            pydantic_tool.organization_id = actor.organization_id
            # Auto-generate description if not provided
            if pydantic_tool.description is None:
                pydantic_tool.description = pydantic_tool.json_schema.get("description", None)
            tool_data = pydantic_tool.model_dump()

            tool = ToolModel(**tool_data)
            tool.create(session, actor=actor)  # Re-raise other database-related errors
        return tool.to_pydantic()

    @enforce_types
    def get_tool_by_id(self, tool_id: str, actor: PydanticUser) -> PydanticTool:
        """Fetch a tool by its ID."""
        with self.session_maker() as session:
            # Retrieve tool by id using the Tool model's read method
            tool = ToolModel.read(db_session=session, identifier=tool_id, actor=actor)
            # Convert the SQLAlchemy Tool object to PydanticTool
            return tool.to_pydantic()

    @enforce_types
    def get_tool_by_name(self, tool_name: str, actor: PydanticUser) -> Optional[PydanticTool]:
        """Retrieve a tool by its name and a user. We derive the organization from the user, and retrieve that tool."""
        try:
            with self.session_maker() as session:
                tool = ToolModel.read(db_session=session, name=tool_name, actor=actor)
                return tool.to_pydantic()
        except NoResultFound:
            return None

    @enforce_types
    def list_tools(self, actor: PydanticUser, cursor: Optional[str] = None, limit: Optional[int] = 50) -> List[PydanticTool]:
        """List all tools with optional pagination using cursor and limit."""
        with self.session_maker() as session:
            tools = ToolModel.list(
                db_session=session,
                cursor=cursor,
                limit=limit,
                organization_id=actor.organization_id,
            )
            return [tool.to_pydantic() for tool in tools]

    @enforce_types
    def update_tool_by_id(self, tool_id: str, tool_update: ToolUpdate, actor: PydanticUser) -> PydanticTool:
        """Update a tool by its ID with the given ToolUpdate object."""
        with self.session_maker() as session:
            # Fetch the tool by ID
            tool = ToolModel.read(db_session=session, identifier=tool_id, actor=actor)

            # Update tool attributes with only the fields that were explicitly set
            update_data = tool_update.model_dump(exclude_none=True)
            for key, value in update_data.items():
                setattr(tool, key, value)

            # If source code is changed and a new json_schema is not provided, we want to auto-refresh the schema
            if "source_code" in update_data.keys() and "json_schema" not in update_data.keys():
                pydantic_tool = tool.to_pydantic()
                new_schema = derive_openai_json_schema(source_code=pydantic_tool.source_code)

                tool.json_schema = new_schema

            # Save the updated tool to the database
            return tool.update(db_session=session, actor=actor).to_pydantic()

    @enforce_types
    def delete_tool_by_id(self, tool_id: str, actor: PydanticUser) -> None:
        """Delete a tool by its ID."""
        with self.session_maker() as session:
            try:
                tool = ToolModel.read(db_session=session, identifier=tool_id, actor=actor)
                tool.hard_delete(db_session=session, actor=actor)
            except NoResultFound:
                raise ValueError(f"Tool with id {tool_id} not found.")

    @enforce_types
    def upsert_base_tools(self, actor: PydanticUser) -> List[PydanticTool]:
        """Add default tools in base.py"""
        module_name = "base"
        full_module_name = f"letta.functions.function_sets.{module_name}"
        try:
            module = importlib.import_module(full_module_name)
        except Exception as e:
            # Handle other general exceptions
            raise e

        functions_to_schema = []
        try:
            # Load the function set
            functions_to_schema = load_function_set(module)
        except ValueError as e:
            err = f"Error loading function set '{module_name}': {e}"
            warnings.warn(err)

        # create tool in db
        tools = []
        for name, schema in functions_to_schema.items():
            if name in BASE_TOOLS + BASE_MEMORY_TOOLS:
                tags = [module_name]
                if module_name == "base":
                    tags.append("letta-base")

                # BASE_MEMORY_TOOLS should be executed in an e2b sandbox
                # so they should NOT be letta_core tools, instead, treated as custom tools
                if name in BASE_TOOLS:
                    tool_type = ToolType.LETTA_CORE
                elif name in BASE_MEMORY_TOOLS:
                    tool_type = ToolType.LETTA_MEMORY_CORE
                else:
                    raise ValueError(f"Tool name {name} is not in the list of base tool names: {BASE_TOOLS + BASE_MEMORY_TOOLS}")

                # create to tool
                tools.append(
                    self.create_or_update_tool(
                        PydanticTool(
                            name=name,
                            tags=tags,
                            source_type="python",
                            tool_type=tool_type,
                        ),
                        actor=actor,
                    )
                )

        return tools
