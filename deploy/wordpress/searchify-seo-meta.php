<?php
/**
 * Plugin Name: Searchify SEO Meta (REST)
 * Description: Lets Searchify write the SEO title, meta description and Open Graph fields of Yoast SEO,
 *              Rank Math and SEOPress through the WordPress REST API with an Application Password.
 *              Without this, WordPress silently ignores those fields and the live title never changes.
 * Version:     1.0.0
 *
 * Install: copy this file to wp-content/mu-plugins/searchify-seo-meta.php (create the folder if needed).
 * Must-use plugins load automatically; there is nothing to activate.
 */

if (!defined('ABSPATH')) {
    exit;
}

add_action('init', function () {
    $keys = array(
        // Yoast SEO
        '_yoast_wpseo_title',
        '_yoast_wpseo_metadesc',
        '_yoast_wpseo_opengraph-title',
        '_yoast_wpseo_opengraph-description',
        // Rank Math
        'rank_math_title',
        'rank_math_description',
        'rank_math_facebook_title',
        'rank_math_facebook_description',
        // SEOPress
        '_seopress_titles_title',
        '_seopress_titles_desc',
        '_seopress_social_fb_title',
        '_seopress_social_fb_desc',
    );
    $types = get_post_types(array('show_in_rest' => true), 'names');
    foreach ($types as $type) {
        foreach ($keys as $key) {
            register_post_meta($type, $key, array(
                'type'          => 'string',
                'single'        => true,
                'show_in_rest'  => true,
                'auth_callback' => function ($allowed, $meta_key, $post_id) {
                    return current_user_can('edit_post', $post_id);
                },
                'sanitize_callback' => 'sanitize_text_field',
            ));
        }
    }
}, 20);

// Yoast caches its rendered output in the Indexables table; rebuild it when Searchify changes a field.
$searchify_refresh_yoast = function ($meta_id, $post_id, $meta_key) {
    if (strpos($meta_key, '_yoast_wpseo_') === 0 && function_exists('YoastSEO')) {
        try {
            $repo = YoastSEO()->classes->get(\Yoast\WP\SEO\Builders\Indexable_Builder::class);
            if ($repo) {
                $repo->build_for_id_and_type($post_id, 'post');
            }
        } catch (\Throwable $e) {
            // Indexables refresh on the next save if this fails.
        }
    }
};
add_action('added_post_meta', $searchify_refresh_yoast, 10, 3);
add_action('updated_post_meta', $searchify_refresh_yoast, 10, 3);
