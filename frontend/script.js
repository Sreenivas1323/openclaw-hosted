/* ═══════════════════════════════════════════
   OpenClaw Hosted — Scripts
   Scroll animations, FAQ, spots counter
   ═══════════════════════════════════════════ */

(function () {
  'use strict';

  // ─── Scroll-triggered fade-in animations ───
  function initScrollAnimations() {
    const elements = document.querySelectorAll('.fade-in');

    // Immediately reveal elements already in view
    const revealIfVisible = (el) => {
      const rect = el.getBoundingClientRect();
      if (rect.top < window.innerHeight * 0.92) {
        el.classList.add('visible');
        return true;
      }
      return false;
    };

    // First pass: reveal anything already on screen
    elements.forEach(revealIfVisible);

    // Intersection Observer for the rest
    const observer = new IntersectionObserver(
      (entries) => {
        entries.forEach((entry) => {
          if (entry.isIntersecting) {
            entry.target.classList.add('visible');
            observer.unobserve(entry.target);
          }
        });
      },
      {
        threshold: 0.08,
        rootMargin: '0px 0px -40px 0px',
      }
    );

    elements.forEach((el) => {
      if (!el.classList.contains('visible')) {
        observer.observe(el);
      }
    });
  }

  // ─── FAQ Accordion ───
  function initFAQ() {
    const items = document.querySelectorAll('.faq-item');

    items.forEach((item) => {
      const toggle = item.querySelector('.faq-toggle');
      const content = item.querySelector('.faq-content');

      toggle.addEventListener('click', () => {
        const isOpen = item.classList.contains('open');

        // Close all others
        items.forEach((other) => {
          if (other !== item && other.classList.contains('open')) {
            other.classList.remove('open');
            const otherContent = other.querySelector('.faq-content');
            otherContent.classList.add('hidden');
            other.querySelector('.faq-toggle').setAttribute('aria-expanded', 'false');
          }
        });

        // Toggle current
        if (isOpen) {
          item.classList.remove('open');
          content.classList.add('hidden');
          toggle.setAttribute('aria-expanded', 'false');
        } else {
          item.classList.add('open');
          content.classList.remove('hidden');
          toggle.setAttribute('aria-expanded', 'true');
        }
      });
    });
  }

  // ─── Spots counter (simulated scarcity) ───
  function initSpotsCounter() {
    const counter = document.getElementById('spots-counter');
    if (!counter) return;

    // Use localStorage to persist the number across visits
    const STORAGE_KEY = 'oc_spots_remaining';
    const STORAGE_TS_KEY = 'oc_spots_ts';

    let spots = 15;

    // Check if we have a stored value
    const stored = localStorage.getItem(STORAGE_KEY);
    const storedTs = localStorage.getItem(STORAGE_TS_KEY);

    if (stored && storedTs) {
      spots = parseInt(stored, 10);
      // Every 12h, reduce by 1 (simulated scarcity)
      const hoursSince = (Date.now() - parseInt(storedTs, 10)) / (1000 * 60 * 60);
      if (hoursSince > 12 && spots > 3) {
        spots = Math.max(3, spots - Math.floor(hoursSince / 12));
        localStorage.setItem(STORAGE_KEY, spots.toString());
        localStorage.setItem(STORAGE_TS_KEY, Date.now().toString());
      }
    } else {
      // First visit: start between 8-15
      spots = Math.floor(Math.random() * 5) + 8; // 8-12
      localStorage.setItem(STORAGE_KEY, spots.toString());
      localStorage.setItem(STORAGE_TS_KEY, Date.now().toString());
    }

    counter.textContent = `Only ${spots} spot${spots !== 1 ? 's' : ''} left`;

    // Add urgency color if low
    if (spots <= 5) {
      counter.classList.add('text-orange-400');
    }
  }

  // ─── Smooth nav highlight on scroll ───
  function initNavHighlight() {
    const sections = document.querySelectorAll('section[id]');
    const navLinks = document.querySelectorAll('nav a[href^="#"]');

    const observer = new IntersectionObserver(
      (entries) => {
        entries.forEach((entry) => {
          if (entry.isIntersecting) {
            const id = entry.target.id;
            navLinks.forEach((link) => {
              if (link.getAttribute('href') === `#${id}`) {
                link.classList.add('text-white');
                link.classList.remove('text-gray-400');
              } else if (link.getAttribute('href').startsWith('#')) {
                link.classList.remove('text-white');
                link.classList.add('text-gray-400');
              }
            });
          }
        });
      },
      {
        threshold: 0.3,
        rootMargin: '-80px 0px -50% 0px',
      }
    );

    sections.forEach((section) => observer.observe(section));
  }

  // ─── Nav background on scroll ───
  function initNavScroll() {
    const nav = document.querySelector('nav');
    let ticking = false;

    window.addEventListener('scroll', () => {
      if (!ticking) {
        requestAnimationFrame(() => {
          if (window.scrollY > 20) {
            nav.style.borderBottomColor = 'rgba(255, 255, 255, 0.08)';
          } else {
            nav.style.borderBottomColor = 'rgba(255, 255, 255, 0.03)';
          }
          ticking = false;
        });
        ticking = true;
      }
    });
  }

  // ─── Initialize everything on DOM ready ───
  document.addEventListener('DOMContentLoaded', () => {
    initScrollAnimations();
    initFAQ();
    initSpotsCounter();
    initNavHighlight();
    initNavScroll();
  });
})();
