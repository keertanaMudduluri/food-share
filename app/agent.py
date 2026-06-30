import os
import datetime
import sys
import re
import json
from zoneinfo import ZoneInfo
from typing import List, Optional
from pydantic import BaseModel, Field

from google.adk.agents import LlmAgent
from google.adk.apps import App
from google.adk.models import Gemini
from google.adk.tools import AgentTool
from google.adk.tools.mcp_tool import McpToolset
from google.adk.tools.mcp_tool.mcp_session_manager import StdioConnectionParams
from google.adk.workflow import Workflow, START, node
from google.adk.events.event import Event
from google.adk.events.request_input import RequestInput
from google.adk.agents.context import Context
from google.genai import types
from mcp import StdioServerParameters

from .config import config

# -----------------------------------------------------------------------------
# Schemas
# -----------------------------------------------------------------------------

class FoodItem(BaseModel):
    name: str = Field(description="Name of the food item")
    quantity: float = Field(description="Quantity or weight of the food item")
    unit: str = Field(description="Unit of measurement (e.g., kg, boxes, servings)")
    expiry_date: str = Field(description="Expiry date or best before date of the food item")

class DonationInventory(BaseModel):
    items: List[FoodItem] = Field(description="List of validated food items in the donation")

class ShelterMatch(BaseModel):
    shelter_name: str
    matched_items: List[str]
    matching_rationale: str
    urgency_level: str  # HIGH, MEDIUM, LOW

class MatchPlan(BaseModel):
    matches: List[ShelterMatch]
    requires_review: bool = Field(description="Whether this match requires manual review (e.g., contains items expiring within 48h, or high quantity)")
    review_reason: Optional[str] = Field(None, description="Reason why review is required")

# -----------------------------------------------------------------------------
# MCP Toolset Configuration
# -----------------------------------------------------------------------------

mcp_tools = McpToolset(
    connection_params=StdioConnectionParams(
        server_params=StdioServerParameters(
            command=sys.executable,
            args=["-m", "app.mcp_server"]
        )
    )
)

# -----------------------------------------------------------------------------
# Sub-Agents
# -----------------------------------------------------------------------------

inventory_agent = LlmAgent(
    name="inventory_agent",
    model=Gemini(model=config.model),
    instruction="""You are an inventory specialist. Extract the list of food items, quantities, units, and expiry dates from the donor's message.
    Validate that the items are safe to donate (not expired or toxic) and format them into the required schema.
    Use the get_current_inventory tool to check what is already logged if needed.
    
    CRITICAL: You must ALWAYS respond with a JSON object matching the DonationInventory schema.
    Do NOT write any conversational text, explanations, or warnings. Even if all items are invalid or unsafe to donate,
    you must still return a valid DonationInventory object with an empty items list (e.g. {"items": []}).
    """,
    output_schema=DonationInventory,
    tools=[mcp_tools]
)

matcher_agent = LlmAgent(
    name="matcher_agent",
    model=Gemini(model=config.model),
    instruction="""You are a matching specialist. Take the validated food inventory and match it against the current needs of local shelters.
    Use the get_active_shelters tool to find active shelters and their needs.
    After formulating the matches, use the log_matched_donation tool to log each matched donation.
    For each match, provide matching rationale and determine the urgency based on expiry dates:
    - urgency_level HIGH if item expires within 7 days
    - urgency_level MEDIUM if item expires within 30 days
    - urgency_level LOW otherwise
    IMPORTANT: Set requires_review=True and provide a review_reason if ANY item expires within 48 hours,
    OR if total quantity exceeds 200 kg / 100 boxes.
    
    CRITICAL: You must ALWAYS respond with a JSON object matching the MatchPlan schema.
    Do NOT write any conversational text, explanations, or warnings. Even if no matches can be made,
    you must still return a valid MatchPlan object with an empty matches list (e.g. {"matches": [], "requires_review": false, "review_reason": null}).
    """,
    output_schema=MatchPlan,
    tools=[mcp_tools]
)

orchestrator_agent = LlmAgent(
    name="orchestrator_agent",
    model=Gemini(model=config.model),
    instruction="""You are the Food Share Coordinator.
    Your job is to coordinate food donations and match them to shelters.
    Follow these steps:
    1. Call inventory_agent to parse and validate the donation.
    2. Call matcher_agent to match the validated donation with local shelter needs.
    3. Output the MatchPlan directly.
    
    CRITICAL: You must ALWAYS respond with a JSON object matching the MatchPlan schema.
    Do NOT write any conversational text, explanations, or warnings. Even if the sub-agents return errors or indicate no matches,
    you must still return a valid MatchPlan object (e.g., {"matches": [], "requires_review": false, "review_reason": null}).
    """,
    tools=[AgentTool(inventory_agent), AgentTool(matcher_agent)],
    output_schema=MatchPlan
)

# -----------------------------------------------------------------------------
# Workflow Nodes
# -----------------------------------------------------------------------------

# List of illegal/unsafe donation items
BANNED_DONATION_ITEMS = ["alcohol", "wine", "beer", "whiskey", "vodka", "drugs", "medication", "prescription", "marijuana"]

# Injection keywords
INJECTION_KEYWORDS = ["ignore previous", "system prompt", "override instructions", "bypass security", "you must ignore"]


def _check_requires_review(input_text: str, match_plan: dict) -> tuple[bool, str | None]:
    """Deterministic override: force requires_review if any date in the input is within 48h."""
    # If LLM already flagged it, keep that decision
    if match_plan.get("requires_review"):
        return True, match_plan.get("review_reason", "Flagged by matcher agent")

    now = datetime.datetime.now(ZoneInfo("UTC"))
    cutoff = now + datetime.timedelta(hours=48)

    # Scan for ISO dates (YYYY-MM-DD) and common relative terms in the input text
    relative_terms = ["tomorrow", "today", "tonight", "expires today", "expires tomorrow"]
    if any(term in input_text.lower() for term in relative_terms):
        return True, "Contains items expiring within 48h (relative date detected in input)"

    iso_date_pattern = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")
    for match in iso_date_pattern.finditer(input_text):
        try:
            expiry = datetime.datetime.strptime(match.group(1), "%Y-%m-%d").replace(tzinfo=ZoneInfo("UTC"))
            if expiry <= cutoff:
                return True, f"Contains items expiring within 48h (expiry: {match.group(1)})"
        except ValueError:
            continue

    return False, None


@node
def security_checkpoint(node_input: types.Content) -> Event:
    text = ""
    # Extract text from content
    if node_input and node_input.parts:
        text = " ".join(part.text for part in node_input.parts if part.text)
    
    audit_log = {
        "timestamp": datetime.datetime.utcnow().isoformat(),
        "event": "security_checkpoint_evaluation",
        "severity": "INFO",
        "status": "pass",
        "redactions": [],
        "failures": []
    }
    
    # 1. Prompt Injection Detection
    detected_injection = [kw for kw in INJECTION_KEYWORDS if kw in text.lower()]
    if detected_injection:
        audit_log["status"] = "fail"
        audit_log["severity"] = "CRITICAL"
        audit_log["failures"].append(f"Prompt injection detected: {detected_injection}")
        print(json.dumps(audit_log))
        return Event(output=types.Content(role="user", parts=[types.Part.from_text(text="[SECURITY FAILURE]")]), route="fail")

    # 2. Domain-Specific Rule: Unsafe/Illegal Items
    detected_banned = [item for item in BANNED_DONATION_ITEMS if item in text.lower()]
    if detected_banned:
        audit_log["status"] = "fail"
        audit_log["severity"] = "WARNING"
        audit_log["failures"].append(f"Banned donation items detected: {detected_banned}")
        print(json.dumps(audit_log))
        return Event(output=types.Content(role="user", parts=[types.Part.from_text(text="[SECURITY FAILURE]")]), route="fail")

    # 3. PII Scrubbing (Email and Phone)
    scrubbed_text = text
    email_regex = r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+"
    phone_regex = r"\b(?:\+?\d{1,3}[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b"
    
    if re.search(email_regex, scrubbed_text):
        scrubbed_text = re.sub(email_regex, "[REDACTED_EMAIL]", scrubbed_text)
        audit_log["redactions"].append("email")
        
    if re.search(phone_regex, scrubbed_text):
        scrubbed_text = re.sub(phone_regex, "[REDACTED_PHONE]", scrubbed_text)
        audit_log["redactions"].append("phone")

    audit_log["details"] = "All security checks passed."
    print(json.dumps(audit_log))
    
    new_content = types.Content(role="user", parts=[types.Part.from_text(text=scrubbed_text)])
    return Event(output=new_content, route="pass")

@node
def security_block(node_input: types.Content):
    yield Event(content=types.Content(role='model', parts=[types.Part.from_text(text="⚠️ Access Denied: Security Checkpoint failed.")]))
    yield Event(output="Security Checkpoint failed.")

@node(rerun_on_resume=True)
async def orchestrator_node(ctx: Context, node_input: types.Content) -> Event:
    # Delegate core coordination task to orchestrator agent
    raw_result = await ctx.run_node(orchestrator_agent, node_input=node_input)
    
    # ctx.run_node returns a Pydantic MatchPlan when output_schema is set.
    # Convert to plain dict so it can be JSON-serialized and stored in state.
    if hasattr(raw_result, "model_dump"):
        match_plan_dict = raw_result.model_dump()
    elif isinstance(raw_result, dict):
        match_plan_dict = raw_result
    else:
        # Fallback: empty plan so workflow doesn't crash
        match_plan_dict = {"matches": [], "requires_review": False, "review_reason": None}

    # Deterministic safety override: check expiry dates independent of LLM judgment
    input_text = ""
    if node_input and node_input.parts:
        input_text = " ".join(p.text for p in node_input.parts if p.text)
    needs_review, review_reason = _check_requires_review(input_text, match_plan_dict)
    if needs_review:
        match_plan_dict["requires_review"] = True
        match_plan_dict["review_reason"] = review_reason

    # Store match plan in workflow state
    ctx.state["match_plan"] = match_plan_dict
    
    # Route based on review requirement
    if match_plan_dict.get("requires_review", False):
        return Event(output=match_plan_dict, route="needs_review")
    else:
        return Event(output=match_plan_dict, route="auto_approved")

@node(rerun_on_resume=True)
async def human_approval(ctx: Context, node_input: dict):
    if not ctx.resume_inputs or "approval_decision" not in ctx.resume_inputs:
        reason = node_input.get("review_reason", "No reason provided")
        yield RequestInput(
            interrupt_id="approval_decision",
            message=f"✋ MATCH REQUIRES REVIEW: {reason}. Do you approve this match? (yes/no)"
        )
        return
    
    decision = ctx.resume_inputs["approval_decision"].strip().lower()
    if decision in ["yes", "y", "approve"]:
        ctx.state["approved"] = True
        yield Event(output={"status": "Approved by coordinator", "plan": ctx.state["match_plan"]})
    else:
        ctx.state["approved"] = False
        yield Event(output={"status": "Rejected by coordinator", "plan": ctx.state["match_plan"]})

@node
def final_output(ctx: Context, node_input):
    # node_input may be a dict (from orchestrator auto-approve) or an approval dict (from human_approval)
    if hasattr(node_input, "model_dump"):
        node_input = node_input.model_dump()
    if not isinstance(node_input, dict):
        node_input = {}

    status = node_input.get("status", "Auto-approved")
    plan = ctx.state.get("match_plan", {})

    # plan may still be a Pydantic model if stored before conversion
    if hasattr(plan, "model_dump"):
        plan = plan.model_dump()
    
    report = f"### 🥗 Donation Match Results\n\n**Status**: {status}\n\n"
    for match in plan.get("matches", []):
        # Each match entry may be a dict or a Pydantic ShelterMatch
        if hasattr(match, "model_dump"):
            match = match.model_dump()
        report += f"- **Shelter**: {match.get('shelter_name')}\n"
        report += f"  - **Matched Items**: {', '.join(match.get('matched_items', []))}\n"
        report += f"  - **Rationale**: {match.get('matching_rationale')}\n"
        report += f"  - **Urgency**: {match.get('urgency_level')}\n\n"
        
    yield Event(content=types.Content(role='model', parts=[types.Part.from_text(text=report)]))
    yield Event(output=report)

# -----------------------------------------------------------------------------
# Workflow Graph
# -----------------------------------------------------------------------------

from google.adk.workflow import Edge

root_agent = Workflow(
    name="food_share_workflow",
    edges=[
        Edge(from_node=START, to_node=security_checkpoint),
        Edge(from_node=security_checkpoint, to_node=orchestrator_node, route="pass"),
        Edge(from_node=security_checkpoint, to_node=security_block, route="fail"),
        Edge(from_node=orchestrator_node, to_node=human_approval, route="needs_review"),
        Edge(from_node=orchestrator_node, to_node=final_output, route="auto_approved"),
        Edge(from_node=human_approval, to_node=final_output)
    ],
    rerun_on_resume=True
)

app = App(
    root_agent=root_agent,
    name="app"
)
