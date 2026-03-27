"""Quick test script to run creative evaluation against sample data.

Usage:
    uv run python -m creative_eval.run_eval_test
"""

import json
import logging

from .config import EvalConfig
from .evaluate import evaluate_creatives

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)

# Sample campaign context
CAMPAIGN_CONTEXT = {
    "brand": "Paul Reed Smith (PRS)",
    "target_product": "PRS SE CE24 Electric Guitar",
    "target_audience": "millennials who follow jam bands (e.g., Widespread Panic and Phish), respond positively to nostalgic messages, and love surreal memes",
    "key_selling_points": "The 85/15 S Humbucker pickups deliver a wide tonal range, from thick humbucker tones to clear single-coil sounds, making the guitar suitable for various genres.",
    "target_search_trend": "tswift engaged",
}

# Sample finalized ad copies (realistic outputs from creative_agent)
SAMPLE_AD_COPIES = [
    {
        "original_id": 1,
        "tone_style": "Humorous",
        "headline": "She Said Yes, You Said Solo",
        "body_text": "While the world celebrates Taylor's ring, celebrate your own commitment — to tone. The PRS SE CE24's 85/15 S pickups go from thick humbuckers to sparkling single-coils faster than you can say 'I do.'",
        "trend_connection": "Playfully ties the Taylor Swift engagement buzz to a musician's commitment to their instrument and sound.",
        "audience_appeal_rationale": "Jam band millennials appreciate clever wordplay and the idea of being 'married' to their craft.",
        "social_caption": "She's got the ring. You've got the riff. #PRS #SE_CE24",
        "call_to_action": "Find your perfect match — shop the SE CE24 now",
        "detailed_performance_rationale": "Combines trending pop culture moment with relatable musician humor. The wordplay creates shareable social content while highlighting the guitar's versatility.",
    },
    {
        "original_id": 2,
        "tone_style": "Aspirational",
        "headline": "Every Love Story Deserves a Soundtrack",
        "body_text": "The world is celebrating love right now. Add your voice to the chorus with the PRS SE CE24 — an instrument built to make every moment legendary, from stadium anthems to intimate serenades.",
        "trend_connection": "Uses the universal love theme sparked by Taylor Swift's engagement to position the guitar as the soundtrack to life's biggest moments.",
        "audience_appeal_rationale": "Appeals to musicians who see their playing as part of life's emotional tapestry, resonating with the nostalgic sensibilities of jam band fans.",
        "social_caption": "Write the soundtrack to your own love story 🎸 #PRS #SE_CE24",
        "call_to_action": "Start your next chapter — explore the SE CE24",
        "detailed_performance_rationale": "Elevates the product by connecting it to universal emotional moments. The aspirational tone aligns with the dream of creating meaningful music.",
    },
    {
        "original_id": 3,
        "tone_style": "Relatable/Meme-based",
        "headline": "POV: Your Guitar is More Reliable Than Any Relationship",
        "body_text": "Trends come and go. Engagements make headlines. But your PRS SE CE24? She's always in tune (unlike your ex). 85/15 pickups that never ghost you.",
        "trend_connection": "Leverages the engagement news cycle with a self-deprecating, meme-ready comparison between relationships and the reliability of a great guitar.",
        "audience_appeal_rationale": "The meme-native, self-deprecating humor resonates strongly with millennials who consume surreal and ironic content.",
        "social_caption": "relationships are temporary. tone is forever. #PRS #SE_CE24 #neverghosts",
        "call_to_action": "Commit to great tone — grab the SE CE24",
        "detailed_performance_rationale": "Meme-format copy is highly shareable on Instagram/TikTok. The contrast between fleeting trends and lasting guitar quality creates memorable, scroll-stopping content.",
    },
]

# Sample finalized visual concepts
SAMPLE_VISUAL_CONCEPTS = [
    {
        "ad_copy_id": 1,
        "concept_name": "The Proposal Riff",
        "trend": "tswift engaged",
        "trend_reference": "A guitarist on one knee presenting a PRS SE CE24 like an engagement ring, mirroring the trending engagement imagery.",
        "markets_product": "Centers the PRS SE CE24 as the focal point, showcasing its elegant design and craftsmanship in a dramatic setting.",
        "audience_appeal": "The humorous subversion of the proposal scene appeals to musicians who joke about their relationship with their instruments.",
        "selection_rationale": "Highest viral potential due to the visual pun. Immediately recognizable reference to the trending topic with clear product placement.",
        "headline": "She Said Yes, You Said Solo",
        "social_caption": "She's got the ring. You've got the riff. #PRS #SE_CE24",
        "call_to_action": "Find your perfect match — shop the SE CE24 now",
        "concept_summary": "A cinematic scene of a guitarist on one knee presenting a PRS SE CE24 to an imaginary audience, spotlight illuminating the guitar's finish like a diamond ring, in a dimly lit concert venue.",
        "image_generation_prompt": "Photorealistic 9:16 vertical portrait of a male musician in his 30s kneeling on a dark concert stage, dramatically presenting a PRS SE CE24 electric guitar as if proposing. The guitar is held at eye level, angled to catch a single warm spotlight that creates a brilliant gleam on its sunburst finish. The background is a blurred, atmospheric concert venue with soft amber bokeh lights suggesting a crowd. Shot with a 85mm telephoto lens, shallow depth of field, cinematic color grading with warm amber tones and deep shadows. Award-winning studio lighting quality, 8K resolution, ultra-detailed texture on the guitar's maple top and chrome hardware.",
    },
    {
        "ad_copy_id": 3,
        "concept_name": "The Ghost-Free Zone",
        "trend": "tswift engaged",
        "trend_reference": "Uses the cultural conversation around relationships and commitment sparked by the engagement news, subverted through meme-culture visual language.",
        "markets_product": "Features the PRS SE CE24 as the 'reliable partner' in a split-screen comparison, highlighting its premium build quality.",
        "audience_appeal": "Meme-native visual format that millennials instantly recognize and share, with ironic humor about relationship reliability vs guitar reliability.",
        "selection_rationale": "Strong meme potential with split-screen format. The visual contrast is immediately engaging and the message is clear without reading the copy.",
        "headline": "POV: Your Guitar is More Reliable Than Any Relationship",
        "social_caption": "relationships are temporary. tone is forever. #PRS #SE_CE24 #neverghosts",
        "call_to_action": "Commit to great tone — grab the SE CE24",
        "concept_summary": "A meme-style split-screen comparing a 'ghosted' phone notification on the left with a pristine PRS SE CE24 bathed in warm light on the right, captioned with relationship vs guitar reliability humor.",
        "image_generation_prompt": "Photorealistic 9:16 vertical split-screen social media meme format. Left half shows a cracked smartphone screen displaying a 'Read 2 days ago' message notification in cold blue light, conveying rejection and ghosting. Right half shows a pristine PRS SE CE24 electric guitar standing upright on a professional guitar stand, bathed in warm golden light from a studio softbox, with the guitar's sunburst finish gleaming. The contrast between cold rejection and warm reliability is stark. Clean white text overlay space at top. Shot in studio with professional product photography lighting, macro detail on guitar hardware and finish, 8K resolution, commercial photography quality.",
    },
]


def main():
    config = EvalConfig()
    print(f"\nRunning creative evaluation with model: {config.eval_model}")
    print(f"Passing threshold: {config.passing_threshold}")
    print(f"Evaluating {len(SAMPLE_AD_COPIES)} ad copies and {len(SAMPLE_VISUAL_CONCEPTS)} visual concepts\n")
    print("=" * 60)

    report = evaluate_creatives(
        campaign_context=CAMPAIGN_CONTEXT,
        ad_copies=SAMPLE_AD_COPIES,
        visual_concepts=SAMPLE_VISUAL_CONCEPTS,
        config=config,
    )

    # Print results
    print("\n" + "=" * 60)
    print("EVALUATION RESULTS")
    print("=" * 60)

    print(f"\n--- Ad Copy Scores ---")
    for ac_eval in report.ad_copy_evaluations:
        status = "PASS" if ac_eval.score.passed else "FAIL"
        print(f"\n  [{status}] #{ac_eval.original_id} \"{ac_eval.headline}\" ({ac_eval.tone_style})")
        print(f"    Overall: {ac_eval.score.overall_score:.1%}")
        for v in ac_eval.score.verdicts:
            v_status = "+" if v.verdict == "pass" else "-"
            print(f"    [{v_status}] {v.dimension}: {v.score}/10 — {v.rationale}")
        if ac_eval.score.strengths:
            print(f"    Strengths: {', '.join(ac_eval.score.strengths)}")
        if ac_eval.score.improvements:
            print(f"    Improve: {', '.join(ac_eval.score.improvements)}")

    print(f"\n--- Visual Concept Scores ---")
    for vc_eval in report.visual_concept_evaluations:
        status = "PASS" if vc_eval.score.passed else "FAIL"
        print(f"\n  [{status}] \"{vc_eval.concept_name}\"")
        print(f"    Overall: {vc_eval.score.overall_score:.1%}")
        for v in vc_eval.score.verdicts:
            v_status = "+" if v.verdict == "pass" else "-"
            print(f"    [{v_status}] {v.dimension}: {v.score}/10 — {v.rationale}")
        if vc_eval.score.strengths:
            print(f"    Strengths: {', '.join(vc_eval.score.strengths)}")
        if vc_eval.score.improvements:
            print(f"    Improve: {', '.join(vc_eval.score.improvements)}")

    print(f"\n--- Summary ---")
    s = report.summary
    print(f"  Ad copies:       {s.ad_copies_passed}/{s.total_ad_copies} passed (avg: {s.avg_ad_copy_score:.1%})")
    print(f"  Visual concepts: {s.visual_concepts_passed}/{s.total_visual_concepts} passed (avg: {s.avg_visual_score:.1%})")
    print(f"  Overall pass rate: {s.overall_pass_rate:.1%}")
    print(f"  Weakest dimensions: {', '.join(s.weakest_dimensions)}")
    print("=" * 60)

    # Save full report
    report_path = "creative_eval_test_report.json"
    with open(report_path, "w") as f:
        f.write(report.model_dump_json(indent=2))
    print(f"\nFull report saved to: {report_path}")


if __name__ == "__main__":
    main()
