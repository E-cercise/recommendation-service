import orjson as json
import numpy as np
import pandas as pd
from sentence_transformers import SentenceTransformer
import config
from utils.text import build_user_text, clean_text
from models.schema import RecommendRequest
import faiss
from utils.equipment import prepare_options_dataframe


# Load model only once
MODEL = SentenceTransformer("all-MiniLM-L6-v2")

# Load & preprocess vector cache
with open("data/vector_cache.json", "rb") as f:
    vector_data = json.loads(f.read())


VECTORS = np.array(list(vector_data.values()), dtype='float32')
IDS = list(vector_data.keys())
ID_TO_INDEX = {k: i for i, k in enumerate(IDS)}

# Build FAISS index once
DIM = VECTORS.shape[1]
index = faiss.IndexFlatIP(DIM)  # Inner Product = cosine if vectors normalized
faiss.normalize_L2(VECTORS)
index.add(VECTORS)

# Load equipment data
with open("data/equipment_options_with_tags.json", "rb") as f:
    EQUIPMENT_DATA = json.loads(f.read())

# Create a fast lookup dict for option_id
OPTION_BY_ID = {str(opt["option_id"]): opt for opt in EQUIPMENT_DATA if "option_id" in opt}

# Utility to extract a flattened version of tag/attribute data for scoring
def build_equipment_text(option):
    tags = [t.get("name", "") for t in option.get("tags", [])]
    attrs = option.get("attribute_values", [])
    return clean_text(" ".join(tags + attrs))

def vectorized_rule_scoring(df: pd.DataFrame, req: RecommendRequest):
    score = np.zeros(len(df))
    explanations = [""] * len(df)  # Track rule explanations


    # Preprocess request

    pref_tags = {p.tag.lower() for p in req.preferences or [] if p.tag}

    goal = (req.goal or "").lower()
    experience = (req.experience or "").lower()
    gender = (req.gender or "").lower()
    user_type = (req.user_type or "").lower()

    # 🔸 Tag matching
    def tag_match(tag_set, tag): return tag in tag_set if tag else False
    def attr_match(attr_set, attr): return attr in attr_set if attr else False

    for i, row in df.iterrows():
        s, reason = 0, []
        
        tag_intersection = pref_tags & row.tags
        s += len(tag_intersection) * 6
        reason.append(f"Matched tags: {', '.join(tag_intersection)} (+{len(tag_intersection)*6})")

        
        text = row.text
        for tag in pref_tags:
            if tag in text:
                s += 3
                reason.append(f"Tag '{tag}' found in text (+3)")


        # Tag/Pref score
        for pref in req.preferences or []:
            tag = (pref.tag or "").lower()
            if tag and tag_match(row.tags, tag):
                s += 6
                reason.append(f"Direct tag match: {tag} (+6)")

            if tag and tag in row.text:
                s += 3
                reason.append(f"Tag in text: {tag} (+3)")

            if tag and pref.group == "muscle" and tag_match(row.tags, tag):
                s += 4
                reason.append(f"Muscle group match: {tag} (+4)")

            if tag and pref.group == "goal" and tag_match(row.tags, tag):
                s += 4
                reason.append(f"Goal group match: {tag} (+4)")

            if pref.max_price and row.price <= pref.max_price:
                s += 5
                reason.append(f"Within price range (≤ {pref.max_price}) (+5)")

            if pref.min_weight and row.weight >= pref.min_weight:
                s += 5
                reason.append(f"Above weight threshold (≥ {pref.min_weight}) (+5)")

        # Attribute scores
        for attr, val in {
            "adjustable": 2, "compact": 2, "portable": 1,
            "foldable": 1, "budget": 1, "multi-function": 2
        }.items():
            if attr in row.attrs:
                s += val
                reason.append(f"Attribute match: {attr} (+{val})")


        # Gender logic
        if gender == "female":
            if any(t in row.tags for t in ["glutes", "core", "abs"]): 
                s += 6
                reason.append("Female: glutes/core/abs match (+6)")

            if any(a in row.attrs for a in ["compact", "adjustable"]):
                s += 3
                reason.append("Female: compact/adjustable attribute (+3)")

        elif gender == "male":
            if any(t in row.tags for t in ["arms", "chest", "pull-up"]): 
                s += 6
                reason.append("Male: arms/chest/pull-up match (+6)")

            if "heavy" in row.text or row.weight >= 60: 
                s += 4
                reason.append("Male: heavy or weight ≥ 60 (+4)")


        # Age logic
        if req.age:
            if req.age >= 50:
                if any(t in row.tags for t in ["low-impact", "joint-friendly", "post-injury"]): 
                    s += 10
                    reason.append("Age ≥ 50: senior-friendly tags (+10)")

                if row.weight < 40: 
                    s += 4
                    reason.append("Age ≥ 50: lightweight (<40) equipment (+4)")

            elif req.age < 18:
                s += 3
                reason.append("Under 18: youth-friendly bias (+3)")


        # Goal → Tag
        goal_tags = {
            "tone": ["bodyweight", "multi-function", "compact"],
            "build-muscle": ["resistance", "weighted", "barbell-compatible"],
            "weight-loss": ["cardio", "endurance", "bodyweight"],
            "rehab": ["low-impact", "joint-friendly", "stretching"],
            "mobility": ["stretching", "flexibility", "balance"],
            "strength": ["weighted", "barbell-compatible", "resistance"],
            "endurance": ["cardio", "row", "treadmill"],
            "flexibility": ["stretching", "mobility"],
            "posture-correction": ["core", "back", "adjustable"],
            "pre/post-natal": ["low-impact", "core", "mobility"],
            "athletic-training": ["cable", "multi-function", "tower"],
            "injury-prevention": ["joint-friendly", "adjustable"],
            "functionality": ["full-body", "multi-function"]
        }
        for tag in goal_tags.get(goal, []):
            if tag in row.tags:
                s += 4
                reason.append(f"Goal tag match: {tag} (+4)")

            if tag in row.attrs:
                s += 2
                reason.append(f"Goal attr match: {tag} (+2)")

  # Experience
        if experience == "beginner" and tag_match(row.tags, "beginner-friendly"):
            s += 6
            reason.append("Experience: beginner-friendly tag (+6)")
        elif experience == "intermediate" and tag_match(row.tags, "intermediate"):
            s += 4
            reason.append("Experience: intermediate tag (+4)")
        elif experience == "advanced" and tag_match(row.tags, "advanced"):
            s += 4
            reason.append("Experience: advanced tag (+4)")
        elif experience == "athlete":
            if tag_match(row.tags, "athlete") or row.weight > 80:
                s += 6
                reason.append("Experience: athlete match or heavy weight (>80) (+6)")
        elif experience == "elderly":
            if any(tag_match(row.tags, t) for t in ["elderly", "joint-friendly"]) or attr_match(row.attrs, "low-impact"):
                s += 8
                reason.append("Experience: elderly-friendly match (+8)")

        # Height/Weight
        if req.weight and req.weight >= 90:
            s += 3
            reason.append("Weight ≥ 90kg (+3)")
        if req.height and req.height >= 190:
            s += 2
            reason.append("Height ≥ 190cm (+2)")

        # User type
        if user_type == "athlete":
            s += 3
            reason.append("User type: athlete (+3)")
        elif user_type == "elderly":
            s += 5
            reason.append("User type: elderly (+5)")
        score[i] = s
        explanations[i] = "; ".join(reason) if reason else "No scoring rules matched"

    return score, explanations

def get_recommendations(req: RecommendRequest):
    user_text = build_user_text(req)
    user_vector = MODEL.encode(user_text, convert_to_tensor=False).reshape(1, -1).astype('float32')
    faiss.normalize_L2(user_vector)

    D, I = index.search(user_vector, k=1000)  # Top K most similar vectors

    # Filter matched options
    matched_options = []
    similarities = []
    for i, sim in zip(I[0], D[0]):
        option_id = IDS[i]
        opt = OPTION_BY_ID.get(option_id)
        if not opt:
            continue
        similarities.append(sim)
        matched_options.append(opt)

    if not matched_options:
        return []

    # Prepare DataFrame for batch scoring
    df = prepare_options_dataframe(matched_options)
    rule_scores, explanations = vectorized_rule_scoring(df, req)

    # Combine scores
    similarities = np.array(similarities)
    df["embedding_similarity"] = similarities * 10
    df["rule_score"] = rule_scores
    df["score"] = df["embedding_similarity"] + df["rule_score"]
    df["rule_explanation"] = explanations


    # Inject scores and optional debug into the original option dict
    for idx, row in df.iterrows():
        opt = df.at[idx, "data"]
        opt["score"] = float(row["score"])
        opt["rule_applied"] = row["rule_explanation"]

        if config.DEBUG:
            opt["__debug"] = {
                "embedding_similarity": round(row["embedding_similarity"], 2),
                "rule_score": round(row["rule_score"], 2),
                "user_text": user_text
            }

    # Deduplicate by equipment_id
    seen = {}
    for opt in sorted(df["data"], key=lambda o: o["score"], reverse=True):
        eq_id = opt.get("equipment_id")
        if eq_id not in seen:
            seen[eq_id] = opt

    return list(seen.values())[:100]