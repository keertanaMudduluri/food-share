import datetime
import json
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("food-share-server")

# In-memory database of active shelters
SHELTERS_DB = [
    {
        "name": "Downtown Rescue Mission",
        "location": "Downtown",
        "accepted_categories": ["canned goods", "protein", "dry goods"],
        "urgent_needs": "canned beans, peanut butter, tuna",
    },
    {
        "name": "Hope Shelter",
        "location": "Northside",
        "accepted_categories": ["fresh produce", "dairy", "bakery"],
        "urgent_needs": "milk, bread, fresh vegetables",
    },
    {
        "name": "Community Food Pantry",
        "location": "Eastside",
        "accepted_categories": ["cereals", "rice", "soup", "canned goods"],
        "urgent_needs": "rice, pasta, tomato soup",
    }
]

INVENTORY_DB = []

@mcp.tool()
def get_active_shelters() -> str:
    """Get the list of active local shelters, their locations, accepted food categories, and urgent needs."""
    return json.dumps(SHELTERS_DB, indent=2)

@mcp.tool()
def get_current_inventory() -> str:
    """Retrieve the current matched food donation log and in-memory food bank inventory."""
    return json.dumps(INVENTORY_DB, indent=2)

@mcp.tool()
def log_matched_donation(donor_name: str, shelter_name: str, item_name: str, quantity: float, unit: str) -> str:
    """Log a matched and approved food donation to a specific shelter.
    
    Args:
        donor_name: The name of the person or store donating.
        shelter_name: The target shelter name receiving the food.
        item_name: The name of the food item.
        quantity: The quantity matched.
        unit: The unit of measurement.
    """
    entry = {
        "donor_name": donor_name,
        "shelter_name": shelter_name,
        "item_name": item_name,
        "quantity": quantity,
        "unit": unit,
        "timestamp": str(datetime.datetime.now())
    }
    INVENTORY_DB.append(entry)
    return f"Successfully logged donation of {quantity} {unit} of {item_name} to {shelter_name}."

if __name__ == "__main__":
    mcp.run(transport="stdio")
