"""Static HTML fragments for the creative gallery report (byte-identical to the
originals previously inlined in ``save_creative_gallery_html``)."""

HTML_TEMPLATE = """<!DOCTYPE html>
        <html lang="en">
        <head>
            <meta charset="UTF-8">
            <meta name="viewport" content="width=device-width, initial-scale=1.0">
            <title>Trend Creative Report</title>
            <style>
                /* Basic body styling for better presentation */
                body {
                    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
                    background-color: #f0f2f5;
                    margin: 0;
                    padding: 20px;
                }

                h1 {
                    text-align: center;
                    color: #333;
                    font-size: 3rem; /* You can adjust this value to your liking */ 
                    margin-bottom: 40px; /* Increased for better spacing */
                }

                /* Research degradation banner (rendered only when a research
                   producer exhausted its retries; empty string otherwise). */
                .research-warning {
                    max-width: 1000px;
                    margin: 0 auto 30px;
                    padding: 16px 24px;
                    background-color: #fff8e1;
                    border: 1px solid #f0c36d;
                    border-left: 6px solid #e0a800;
                    border-radius: 8px;
                    color: #5c4400;
                    font-size: 1rem;
                }

                .research-warning ul {
                    margin: 8px 0 0;
                    padding-left: 20px;
                }

                /* Sub-header styles */
                .sub-header-container {
                    max-width: 1000px; /* Step 1: Match the gallery's width for perfect alignment */
                    margin: 0 auto 40px;
                    display: flex;
                    gap: 40px; /* You can adjust this gap to control the spacing between the columns */
                    list-style: none;
                    padding: 0;
                }

                .sub-header-container h3 {
                    flex: 1; /* make each h3 take up an equal amount of space */
                    margin: 0;
                    font-weight: 500;
                    color: #555;
                    cursor: pointer;
                    transition: all 0.3s ease;
                    text-align: center;
                    
                    /* Optional but nice: Add some padding and a background to visualize the equal dimensions */
                    background-color: #fff;
                    padding: 20px;
                    border-radius: 8px;
                    box-shadow: 0 4px 8px rgba(0,0,0,0.05);
                }

                /* this rule styles the label to match the hover-text style */
                .sub-header-container h3 strong {
                    display: block; /* This makes the label appear on its own line */
                    color: #a0d8ff; /* This is the light blue color from the hover-snippet */
                    font-weight: 600;
                    margin-bottom: 4px; /* Adds a little space between the label and the text */
                }

                .sub-header-container h3:hover {
                    transform: scale(1.05);
                    box-shadow: 0 8px 16px rgba(0,0,0,0.1); /* Enhance the shadow for a "lift" effect */
                    background-color: #f9f9f9;
                }
                .sub-header-container h3:hover,
                .sub-header-container h3:hover strong {
                    color: #007bff;
                }

                /* --- CSS RULE TO ENLARGE MIDDLE HEADER'S TEXT --- */
                .sub-header-container h3:nth-child(2) .enlarged-text {
                    font-size: 1.5em;
                    font-weight: 600;
                }

                /* --- THIS IS THE CRITICAL RULE --- */
                .gallery-container {
                    display: flex; /* Switch from Grid to Flexbox */
                    flex-wrap: wrap; /* Allow items to wrap to the next line */
                    justify-content: center; /* This centers the items, including the last row */
                    gap: 20px;
                    /* Cap at two 480px columns (2*480 + 20px gap = 980) so the gallery never
                       grows to a third column on ultra-wide screens. */
                    max-width: 1000px;
                    margin: 0 auto;
                }

                /* --- MODIFIED .gallery-item RULE --- */
                .gallery-item {
                    /* Fixed-width cards: each holds one 480px-square image and does NOT grow
                       or shrink with the window. Two sit side-by-side; they wrap to a single
                       column only when the viewport is too narrow for two. max-width keeps a
                       card from overflowing on very small screens. */
                    flex: 0 0 480px;
                    max-width: 100%;

                    display: flex;
                    flex-direction: column;
                    overflow: hidden;
                    border-radius: 8px;
                    box-shadow: 0 4px 8px rgba(0, 0, 0, 0.1);
                    background-color: #fff;
                    transition: transform 0.3s ease, box-shadow 0.3s ease;
                }

                .gallery-item:hover {
                    transform: translateY(-5px);
                    box-shadow: 0 8px 16px rgba(0, 0, 0, 0.2);
                }

                .image-title {
                    margin: 0;
                    padding: 15px;
                    font-size: 1.5em;
                    font-weight: 600;
                    text-align: center;
                    color: #333;
                    background-color: #f9f9f9;
                    border-bottom: 1px solid #eee;
                }

                .image-container {
                    position: relative;
                    overflow: hidden;
                }

                .image-container img {
                    width: 100%;
                    /* Fixed square display (480x480 inside the fixed-width card), independent
                       of the source image's exact dimensions, so images never balloon on wide
                       windows. object-fit: cover fills the square without distortion. */
                    aspect-ratio: 1 / 1;
                    object-fit: cover;
                    display: block;
                    transition: transform 0.3s ease;
                    cursor: pointer;
                }

                /* The zoom effect is now triggered by hovering the image container */
                .gallery-item:hover .image-container img {
                    transform: scale(1.30);
                }

                /* Styling for the caption */
                .caption {
                    margin: 0;
                    padding: 15px;
                    font-size: 1.20em;
                    font-weight: normal;
                    text-align: left;
                    color: #444;
                    border-top: 1px solid #eee;
                }

                /* 4. Styling for the hover text */
                .hover-text {
                    position: absolute;
                    top: 0;
                    left: 0;
                    width: 100%;
                    height: 100%;
                    opacity: 0;
                    visibility: hidden;
                    background-color: rgba(0, 0, 0, 0.7);
                    color: white;
                    padding: 20px;
                    box-sizing: border-box;
                    transition: opacity 0.3s ease, visibility 0.3s ease;

                    /* Make the overlay a grid container */
                    display: grid;
                    grid-template-columns: 1fr 1fr;
                    grid-template-rows: 1fr 1fr;

                    /* The overlay will now ignore mouse clicks. */
                    pointer-events: none;
                }

                .gallery-item:hover .hover-text {
                    opacity: 1;
                    visibility: visible;
                }

                /* --- NEW RULES --- */
                .hover-snippet {
                    font-size: 1.1em;
                    line-height: 1.4;
                }

                .hover-snippet strong {
                    display: block;
                    color: #a0d8ff;
                    font-weight: 600;
                }

                .snippet-top-left {
                    justify-self: start; /* Horizontal alignment */
                    align-self: start;  /* Vertical alignment */
                }

                .snippet-top-right {
                    justify-self: end; /* Align horizontally to the end (right) of the grid cell */
                    align-self: start; /* Align vertically to the start (top) of the grid cell */
                    text-align: right; /* Ensure the text itself is right-aligned */
                    
                    /* Explicitly place this in the top-right grid cell (row 1, column 2) */
                    grid-row: 1 / 2;
                    grid-column: 2 / 3;
                }

                /* -- NEW RULE FOR THE THIRD SNIPPET -- */
                .snippet-bottom-left {
                    justify-self: start;
                    align-self: end;
                    grid-row: 2 / 3;
                    grid-column: 1 / 2;
                }

                .snippet-bottom-right {
                    justify-self: end; /* Horizontal alignment */
                    align-self: end; /* Vertical alignment */
                    text-align: right;

                    /* Put this snippet in the bottom-right cell of our 2x2 grid */
                    grid-column: 2 / 3;
                    grid-row: 2 / 3;
                }

                /* Lightbox styles - CORRECTED */
                .lightbox-overlay {
                    position: fixed;
                    top: 0;
                    left: 0;
                    width: 100%;
                    height: 100%;
                    background-color: rgba(0, 0, 0, 0.85);
                    z-index: 1000;
                    display: flex;
                    justify-content: center;
                    align-items: center;
                    opacity: 0;
                    visibility: hidden;
                    transition: opacity 0.3s ease, visibility 0.3s ease;
                }

                .lightbox-overlay.visible {
                    opacity: 1;
                    visibility: visible;
                }

                .lightbox-content {
                    max-width: 95vw;
                    max-height: 90vh;
                    display: block;
                    border-radius: 5px;
                    box-shadow: 0 5px 20px rgba(0, 0, 0, 0.5);
                }

                .lightbox-close {
                    position: absolute;
                    top: 20px;
                    right: 30px;
                    color: white;
                    font-size: 40px;
                    font-weight: bold;
                    cursor: pointer;
                    transition: color 0.2s ease;
                }

                .lightbox-close:hover {
                    color: #ccc;
                }

                /* --- NEW CSS FOR AD COPY & VISUAL CONCEPTS SECTIONS --- */
                .content-section {
                    /* padding: 40px 20px; */
                    max-width: 1000px; /* Match the gallery/sub-header width for aligned edges */
                    margin: 15px auto 0; /* Adds space above the section and centers it */
                }

                /* --- NEW STYLES FOR COLLAPSIBLE BEHAVIOR --- */
                .content-section details {
                    background-color: #fff;
                    border-radius: 8px;
                    box-shadow: 0 4px 8px rgba(0,0,0,0.1);
                    transition: all 0.3s ease;
                }

                .content-section summary {
                    display: flex;
                    align-items: center;
                    justify-content: space-between;
                    padding: 20px 25px;
                    cursor: pointer;
                    list-style: none; /* Hide default triangle */
                }

                .content-section summary::-webkit-details-marker {
                    display: none; /* Hide default triangle in Webkit */
                }

                .content-section summary h2 {
                    font-size: 2em;
                    color: #333;
                    margin: 0; /* Remove default margin */
                }

                .content-section summary::after {
                    content: '+';
                    font-size: 2.5rem;
                    font-weight: 300;
                    color: #007bff;
                    transition: transform 0.2s ease;
                }

                .content-section details[open] > summary::after {
                    content: '−';
                    transform: rotate(180deg);
                }

                /* This grid style now applies to BOTH sections */
                .card-grid {
                    display: grid;
                    grid-template-columns: repeat(2, 1fr);
                    gap: 40px; /* increase from 25px for more space */
                    padding: 0 25px 25px 25px;
                }

                /* This card style now applies to ALL cards in BOTH sections */
                .content-card {
                    background-color: #fff;
                    border-radius: 8px;
                    padding: 25px;
                    box-shadow: 0 4px 8px rgba(0, 0, 0, 0.1);
                    transition: transform 0.3s ease, box-shadow 0.3s ease; /* Smooth transition for the hover effect */
                    font-size: 1.1rem; /* Sets the base font size for everything inside the card */
                }

                .content-card:hover {
                    transform: scale(1.03); /* A subtle scale effect that won't run off the page */
                    box-shadow: 0 8px 20px rgba(0, 0, 0, 0.15);
                }

                /* The DL/DT/DD styles are specific to the card content */
                .content-card dl {
                    display: grid;
                    grid-template-columns: auto 1fr; /* Create two columns: one for the label, one for the text */
                    row-gap: 16px;
                    column-gap: 10px;
                    margin: 0; /* Remove default margins from the <dl> */
                }

                .content-card dt {
                    font-weight: bold; /* bold labeling styling */
                    color: #d9534f; /* A nice, readable red */
                }

                .content-card dd {
                    margin-left: 0; /* Resets browser default indentation */
                    color: #555;
                    line-height: 1.5;
                }
            </style>
        </head>
        <body>
        """

HTML_POST_GALLERY = """
        </div>

        <!-- NEW: Lightbox HTML Structure -->
        <div id="lightbox" class="lightbox-overlay">
            <span class="lightbox-close">&times;</span>
            <img class="lightbox-content" id="lightbox-img">
        </div>
        """

HTML_PRE_VS = """
        <!-- --- NEW HTML FOR VISUAL CONCEPTS SECTION --- -->
        <section class="content-section">
            <details>
                <summary>
                    <h2>Visual Concepts</h2>
                </summary>
                <div class="card-grid">
        """

HTML_POST_VS = """
                </div>
            </details>
        </section>
        """

HTML_PRE_AD_COPY = """
        <!-- --- NEW HTML FOR AD COPY SECTION --- -->
        <section class="content-section">
            <details>
                <summary>
                    <h2>Ad Copy Ideas</h2>
                </summary>
                <div class="card-grid">

        """

HTML_POST_AD_COPY = """
                </div>
            </details>
        </section>
        <!-- --- END OF NEW HTML --- -->
        """

HTML_END_JAVASCRIPT = """
            <!-- NEW: JavaScript for Lightbox functionality -->
            <script>
                document.addEventListener('DOMContentLoaded', () => {
                    const galleryImages = document.querySelectorAll('.image-container img');
                    const lightbox = document.getElementById('lightbox');
                    const lightboxImg = document.getElementById('lightbox-img');
                    const closeBtn = document.querySelector('.lightbox-close');

                    galleryImages.forEach(image => {
                        image.addEventListener('click', () => {
                            // lightboxImg.src = image.src;
                            // Use the 'data-high-res-src' for the lightbox image
                            lightboxImg.src = image.dataset.highResSrc;
                            lightbox.classList.add('visible');
                        });
                    });

                    const closeLightbox = () => lightbox.classList.remove('visible');
                    closeBtn.addEventListener('click', closeLightbox);
                    lightbox.addEventListener('click', e => (e.target === lightbox) && closeLightbox());
                    document.addEventListener('keydown', e => (e.key === 'Escape') && closeLightbox());
                });
            </script>

        </body>
        </html>
        """
