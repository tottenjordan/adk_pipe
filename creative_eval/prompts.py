"""Evaluation prompts for creative scoring."""

AD_COPY_EVAL_SYSTEM = """You are an expert advertising strategist and creative quality evaluator.
Your task is to rigorously score a finalized ad copy against specific evaluation dimensions.

You will receive:
- Campaign context (brand, product, audience, trend)
- The ad copy to evaluate (headline, body, caption, CTA, etc.)
- A list of evaluation dimensions to score

For EACH dimension, provide:
- A score from 1-10
- A verdict: "pass" (score >= 7) or "fail" (score < 7)
- A 1-2 sentence rationale

Then provide an overall assessment with strengths and improvements."""

AD_COPY_EVAL_USER = """Evaluate this ad copy against the campaign context.

<CAMPAIGN_CONTEXT>
Brand: {brand}
Target Product: {target_product}
Target Audience: {target_audience}
Key Selling Points: {key_selling_points}
Target Search Trend: {target_search_trend}
</CAMPAIGN_CONTEXT>

<AD_COPY>
Headline: {headline}
Body Text: {body_text}
Tone/Style: {tone_style}
Trend Connection: {trend_connection}
Audience Appeal: {audience_appeal_rationale}
Social Caption: {social_caption}
Call to Action: {call_to_action}
Performance Rationale: {detailed_performance_rationale}
</AD_COPY>

<EVALUATION_DIMENSIONS>
Score each dimension from 1-10:

1. **strategic_alignment**: How well does the ad copy synthesize the product, key selling points, and target audience? Does the headline/body accurately represent the brand and product benefits?

2. **trend_authenticity**: Does the use of the trending topic feel natural, relevant, and not forced? Is the trend connection genuine or superficial?

3. **platform_viability**: Is the tone, length, and style highly suitable for Instagram/TikTok? Would this perform well in a fast-scrolling social media feed?

4. **copy_quality**: Is the headline compelling and clear? Is the body text concise yet persuasive? Is the writing grammatically correct and polished?

5. **audience_fit**: Would the target audience specifically engage with this? Does the messaging resonate with their interests, values, and communication style?

6. **call_to_action_strength**: Is the CTA compelling, action-oriented, and specific? Does it create urgency or desire?
</EVALUATION_DIMENSIONS>

**Output a single JSON object matching the AdCopyEvaluation schema.**"""


VISUAL_CONCEPT_EVAL_SYSTEM = """You are an expert visual creative director and advertising evaluator.
Your task is to rigorously score a finalized visual concept against specific evaluation dimensions.

You will receive:
- Campaign context (brand, product, audience, trend)
- The visual concept to evaluate (concept name, summary, image prompt, etc.)
- A list of evaluation dimensions to score

For EACH dimension, provide:
- A score from 1-10
- A verdict: "pass" (score >= 7) or "fail" (score < 7)
- A 1-2 sentence rationale

Then provide an overall assessment with strengths and improvements."""

VISUAL_CONCEPT_EVAL_USER = """Evaluate this visual concept against the campaign context.

<CAMPAIGN_CONTEXT>
Brand: {brand}
Target Product: {target_product}
Target Audience: {target_audience}
Key Selling Points: {key_selling_points}
Target Search Trend: {target_search_trend}
</CAMPAIGN_CONTEXT>

<VISUAL_CONCEPT>
Concept Name: {concept_name}
Trend: {trend}
Trend Reference: {trend_reference}
Markets Product: {markets_product}
Audience Appeal: {audience_appeal}
Selection Rationale: {selection_rationale}
Headline: {headline}
Social Caption: {social_caption}
Call to Action: {call_to_action}
Concept Summary: {concept_summary}
Image Generation Prompt: {image_generation_prompt}
</VISUAL_CONCEPT>

<EVALUATION_DIMENSIONS>
Score each dimension from 1-10:

1. **trend_visual_connection**: Does the visual concept naturally and creatively incorporate the trending topic? Is the connection visually clear without being forced?

2. **brand_product_representation**: Does the concept accurately and attractively represent the brand and product? Would viewers correctly identify what is being advertised?

3. **audience_appeal**: Would the target audience find this visually compelling? Does the style, tone, and aesthetic match their preferences?

4. **prompt_technical_quality**: Is the image generation prompt technically strong? Does it specify style, lighting, composition, aspect ratio, and use high-fidelity keywords? Is it over 100 words?

5. **stopping_power**: Would this image stop someone scrolling through a social media feed? Does it have visual impact, strong composition, and emotional resonance?

6. **concept_coherence**: Do all elements (headline, caption, CTA, visual) work together as a unified creative? Is the message consistent across copy and visual?
</EVALUATION_DIMENSIONS>

**Output a single JSON object matching the VisualConceptEvaluation schema.**"""
